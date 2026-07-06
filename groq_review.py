import argparse
import json
import os
import re
import ssl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import truststore
from dotenv import load_dotenv
from groq import DefaultHttpxClient, Groq


DEFAULT_MODEL = "llama-3.3-70b-versatile"
MAX_DIFF_CHARS = 14000


@dataclass
class AIReview:
    risk_level: str
    summary: str
    possible_issues: list[str]
    suggested_commit_message: str
    push_recommendation: str
    next_steps: list[str]
    raw_response: str

    @property
    def is_high_risk(self) -> bool:
        return self.risk_level.lower() == "high" or self.push_recommendation == "do_not_push"

    @property
    def has_no_diff(self) -> bool:
        return self.summary == "No staged or unstaged diff was found."


@dataclass
class AIGitHelp:
    category: str
    plain_english: str
    likely_cause: str
    safe_next_steps: list[str]
    commands_to_consider: list[str]
    warning: str
    raw_response: str


def run_git(repo: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=repo, capture_output=True, text=True)


def git_output(repo: Path, command: list[str]) -> str:
    completed = run_git(repo, command)
    return completed.stdout.strip()


def ensure_git_repo(repo: Path) -> None:
    completed = run_git(repo, ["git", "rev-parse", "--is-inside-work-tree"])
    if completed.returncode != 0:
        raise RuntimeError(f"Not a Git repository: {repo}")


def changed_file_status(repo: Path) -> str:
    return git_output(repo, ["git", "status", "--short"]) or "No changed files."


def diff_for_review(repo: Path) -> tuple[str, str]:
    staged_diff = git_output(repo, ["git", "diff", "--cached", "--no-ext-diff", "--unified=3"])
    if staged_diff:
        return "staged", staged_diff

    unstaged_diff = git_output(repo, ["git", "diff", "--no-ext-diff", "--unified=3"])
    if unstaged_diff:
        return "unstaged", unstaged_diff

    return "none", ""


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n\n[diff truncated for review]\n", True


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Groq response did not contain a JSON object.")
    return json.loads(match.group(0))


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def is_unsafe_ai_command(command: str) -> bool:
    lowered = command.lower()
    unsafe_markers = (
        "reset --hard",
        "clean -fd",
        "clean -xdf",
        "checkout --",
        "push --force",
        "push -f",
        "sslverify false",
        "http.sslverify false",
    )
    return any(marker in lowered for marker in unsafe_markers)


def safe_ai_commands(value: Any) -> list[str]:
    return [command for command in normalize_string_list(value) if not is_unsafe_ai_command(command)]


def review_from_json(data: dict[str, Any], raw_response: str) -> AIReview:
    risk_level = str(data.get("risk_level", "medium")).strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "medium"

    push_recommendation = str(data.get("push_recommendation", "review_first")).strip().lower()
    if push_recommendation not in {"safe_to_push", "review_first", "do_not_push"}:
        push_recommendation = "review_first"

    return AIReview(
        risk_level=risk_level,
        summary=str(data.get("summary", "No summary returned.")).strip(),
        possible_issues=normalize_string_list(data.get("possible_issues")),
        suggested_commit_message=str(data.get("suggested_commit_message", "Update project files")).strip(),
        push_recommendation=push_recommendation,
        next_steps=normalize_string_list(data.get("next_steps")),
        raw_response=raw_response,
    )


def git_help_from_json(data: dict[str, Any], raw_response: str) -> AIGitHelp:
    return AIGitHelp(
        category=str(data.get("category", "UNKNOWN_FAILURE")).strip() or "UNKNOWN_FAILURE",
        plain_english=str(data.get("plain_english", "No explanation returned.")).strip(),
        likely_cause=str(data.get("likely_cause", "Unknown cause.")).strip(),
        safe_next_steps=normalize_string_list(data.get("safe_next_steps")),
        commands_to_consider=safe_ai_commands(data.get("commands_to_consider")),
        warning=str(data.get("warning", "Do not run commands you do not understand.")).strip(),
        raw_response=raw_response,
    )


def groq_client() -> Groq:
    load_dotenv(Path(__file__).with_name(".env"))
    if not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is missing. Add it to C:\\GitHapticController\\.env.")
    ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return Groq(
        api_key=os.environ["GROQ_API_KEY"],
        http_client=DefaultHttpxClient(verify=ssl_context),
    )


def call_groq(messages: list[dict[str, str]], model: str) -> str:
    completion = groq_client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
    )
    return completion.choices[0].message.content or ""


def check_connection(model: str) -> str:
    return call_groq(
        [
            {
                "role": "user",
                "content": "Reply with exactly: Groq connected",
            }
        ],
        model,
    ).strip()


def review_repo(repo: Path, model: str = DEFAULT_MODEL, max_diff_chars: int = MAX_DIFF_CHARS) -> AIReview:
    repo = repo.resolve()
    ensure_git_repo(repo)

    diff_kind, diff_text = diff_for_review(repo)
    status = changed_file_status(repo)
    if not diff_text:
        return AIReview(
            risk_level="low",
            summary="No staged or unstaged diff was found.",
            possible_issues=[],
            suggested_commit_message="Update project files",
            push_recommendation="review_first",
            next_steps=["Stage or edit files before requesting an AI review."],
            raw_response="",
        )

    diff_text, was_truncated = truncate_text(diff_text, max_diff_chars)
    truncation_note = "The diff was truncated." if was_truncated else "The full diff is included."

    system_prompt = (
        "You are a cautious Git diff reviewer for a hardware-assisted developer workflow. "
        "Do not suggest running destructive commands. Do not claim you tested the code. "
        "Return only valid JSON."
    )
    user_prompt = f"""
Review this Git diff for push readiness.

Repository: {repo}
Diff kind: {diff_kind}
Status:
{status}

{truncation_note}

Return this JSON shape only:
{{
  "risk_level": "low | medium | high",
  "summary": "1-2 sentence summary",
  "possible_issues": ["short issue", "..."],
  "suggested_commit_message": "imperative commit message under 72 chars",
  "push_recommendation": "safe_to_push | review_first | do_not_push",
  "next_steps": ["short next step", "..."]
}}

Diff:
```diff
{diff_text}
```
""".strip()

    raw = call_groq(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model,
    )
    return review_from_json(extract_json_object(raw), raw)


def explain_git_failure(
    repo: Path,
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    category: str | None = None,
    safe_command: list[str] | None = None,
    model: str = DEFAULT_MODEL,
) -> AIGitHelp:
    repo = repo.resolve()
    status = changed_file_status(repo)
    safe_command_text = " ".join(safe_command) if safe_command else "None"
    category_text = category or "UNKNOWN_FAILURE"

    system_prompt = (
        "You explain Git pull/push/version-control failures for a beginner using a "
        "hardware controller. Be calm, concrete, and safe. Do not suggest destructive "
        "commands such as reset --hard, clean -fd, checkout --, or force push. Do not "
        "suggest disabling SSL verification. Return only valid JSON."
    )
    user_prompt = f"""
Explain this Git failure and suggest safe next steps.

Repository: {repo}
Command: {" ".join(command)}
Exit code: {returncode}
Detected category from hardcoded rules: {category_text}
Safe command already approved by app rules, if any: {safe_command_text}

Current short status:
{status}

stdout:
```text
{stdout.strip() or "[empty]"}
```

stderr:
```text
{stderr.strip() or "[empty]"}
```

Return this JSON shape only:
{{
  "category": "short uppercase category",
  "plain_english": "short explanation for a beginner",
  "likely_cause": "what probably caused it",
  "safe_next_steps": ["safe step", "..."],
  "commands_to_consider": ["non-destructive command", "..."],
  "warning": "one safety warning"
}}
""".strip()

    raw = call_groq(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model,
    )
    return git_help_from_json(extract_json_object(raw), raw)


def print_review(review: AIReview) -> None:
    print("\n--- Groq Review ---")
    print(f"Risk: {review.risk_level}")
    print(f"Push recommendation: {review.push_recommendation}")
    print(f"Suggested commit message: {review.suggested_commit_message}")
    print(f"\nSummary: {review.summary}")

    if review.possible_issues:
        print("\nPossible issues:")
        for issue in review.possible_issues:
            print(f"- {issue}")

    if review.next_steps:
        print("\nNext steps:")
        for step in review.next_steps:
            print(f"- {step}")


def print_git_help(help_text: AIGitHelp) -> None:
    print("\n--- Groq Git Help ---")
    print(f"Category: {help_text.category}")
    print(f"\nWhat happened: {help_text.plain_english}")
    print(f"Likely cause: {help_text.likely_cause}")

    if help_text.safe_next_steps:
        print("\nSafe next steps:")
        for step in help_text.safe_next_steps:
            print(f"- {step}")

    if help_text.commands_to_consider:
        print("\nCommands to consider manually:")
        for command in help_text.commands_to_consider:
            print(f"- {command}")

    if help_text.warning:
        print(f"\nWarning: {help_text.warning}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Use Groq to review Git changes safely.")
    parser.add_argument(
        "command",
        choices=["check", "review", "explain"],
        help="Run an API health check, review the current Git diff, or explain a Git failure.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        type=Path,
        help="Repository path. Defaults to current directory.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
        help="Groq model ID.",
    )
    parser.add_argument(
        "--max-diff-chars",
        default=MAX_DIFF_CHARS,
        type=int,
        help="Maximum diff characters to send to Groq.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable review JSON.",
    )
    parser.add_argument(
        "--action",
        choices=["pull", "push"],
        help="For `explain`: run this Git command and explain it only if it fails.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        if args.command == "check":
            response = check_connection(args.model)
            print(response)
            return 0

        if args.command == "explain":
            if not args.action:
                raise RuntimeError("Use --action pull or --action push with `explain`.")
            command = ["git", args.action]
            completed = run_git(args.repo.resolve(), command)
            if completed.returncode == 0:
                print(f"`{' '.join(command)}` succeeded. No Git failure to explain.")
                return 0
            help_text = explain_git_failure(
                repo=args.repo,
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                model=args.model,
            )
            if args.json:
                print(json.dumps(help_text.__dict__ | {"raw_response": help_text.raw_response}, indent=2))
            else:
                print_git_help(help_text)
            return completed.returncode

        review = review_repo(args.repo, model=args.model, max_diff_chars=args.max_diff_chars)
        if args.json:
            print(
                json.dumps(
                    {
                        "risk_level": review.risk_level,
                        "summary": review.summary,
                        "possible_issues": review.possible_issues,
                        "suggested_commit_message": review.suggested_commit_message,
                        "push_recommendation": review.push_recommendation,
                        "next_steps": review.next_steps,
                    },
                    indent=2,
                )
            )
        else:
            print_review(review)
        return 0
    except Exception as error:
        print(f"Groq review failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
