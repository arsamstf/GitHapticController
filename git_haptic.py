import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import serial

from groq_review import explain_git_failure, print_git_help
from haptic_controller import HapticController, load_config


GIT_ACTIONS = {
    "status": ["git", "status"],
    "pull": ["git", "pull"],
    "push": ["git", "push"],
}

MERGE_CONFLICT_MARKERS = (
    "merge conflict",
    "fix conflicts",
    "unmerged paths",
    "you have unmerged paths",
    "automatic merge failed",
    "conflict (",
    "both modified:",
)


@dataclass
class GitResult:
    action: str
    command: list[str]
    repo: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"

    @property
    def has_merge_conflict(self) -> bool:
        output = self.combined_output.lower()
        return any(marker in output for marker in MERGE_CONFLICT_MARKERS)


def run_git_action(action: str, repo: Path) -> GitResult:
    command = GIT_ACTIONS[action]
    completed = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return GitResult(
        action=action,
        command=command,
        repo=repo,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def print_git_result(result: GitResult) -> None:
    command_text = " ".join(result.command)
    print(f"Repo: {result.repo}")
    print(f"Command: {command_text}")
    print(f"Exit code: {result.returncode}")

    if result.stdout.strip():
        print("\n--- stdout ---")
        print(result.stdout.rstrip())

    if result.stderr.strip():
        print("\n--- stderr ---")
        print(result.stderr.rstrip())


def play_feedback(result: GitResult, config_path: Path) -> int:
    try:
        config = load_config(config_path)
        with HapticController(config) as controller:
            if result.succeeded and result.action == "push":
                controller.push_success()
            elif result.succeeded:
                controller.success()
            elif result.has_merge_conflict:
                controller.merge_conflict()
            else:
                controller.failure()
    except serial.SerialException as error:
        print(f"\nHaptic feedback failed: {error}", file=sys.stderr)
        return 1

    return 0


def print_ai_git_help(result: GitResult, enabled: bool) -> None:
    if not enabled or result.succeeded or result.action not in {"pull", "push"}:
        return

    print("\nAsking Groq to explain the Git failure...")
    try:
        help_text = explain_git_failure(
            repo=result.repo,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except Exception as error:
        print(f"Groq Git help failed: {error}", file=sys.stderr)
        return

    print_git_help(help_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Git commands with FRDM-MCXN947 haptic feedback.")
    parser.add_argument(
        "action",
        choices=sorted(GIT_ACTIONS),
        help="Git action to run.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        type=Path,
        help="Repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to haptic controller config.json.",
    )
    parser.add_argument(
        "--no-haptic",
        action="store_true",
        help="Run the Git command and print output without sending motor feedback.",
    )
    parser.add_argument(
        "--ai-help",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Ask Groq to explain failed pull/push operations.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    repo = args.repo.resolve()
    if not repo.exists() or not repo.is_dir():
        print(f"Repository path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2

    result = run_git_action(args.action, repo)
    print_git_result(result)

    haptic_failed = 0
    if not args.no_haptic:
        haptic_failed = play_feedback(result, args.config)

    print_ai_git_help(result, args.ai_help)

    if result.succeeded:
        return haptic_failed
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
