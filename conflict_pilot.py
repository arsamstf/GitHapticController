import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial

from git_haptic import GitResult, print_git_result
from groq_review import explain_git_failure, print_git_help
from haptic_controller import find_serial_port, load_config
from touch_git_controller import (
    SharedSerialHaptics,
    TOUCH_PRESSED_PATTERN,
    TOUCH_RELEASED_PATTERN,
)


GIT_ACTIONS = {
    "pull": ["git", "pull"],
    "push": ["git", "push"],
}

NO_UPSTREAM_BRANCH = "NO_UPSTREAM_BRANCH"
PUSH_REJECTED_REMOTE_CHANGES = "PUSH_REJECTED_REMOTE_CHANGES"
MERGE_CONFLICT = "MERGE_CONFLICT"
REBASE_IN_PROGRESS = "REBASE_IN_PROGRESS"
UNCOMMITTED_CHANGES_BLOCKING_PULL = "UNCOMMITTED_CHANGES_BLOCKING_PULL"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


@dataclass
class RecoveryAnalysis:
    category: str
    explanation: str
    safe_command: list[str] | None = None


@dataclass
class GestureConfig:
    long_press_seconds: float = 0.35
    double_tap_window_seconds: float = 0.25


def run_git_command(action: str, command: list[str], repo: Path) -> GitResult:
    print(f"\nAbout to run: {' '.join(command)}")
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    return GitResult(
        action=action,
        command=command,
        repo=repo,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_git_action(action: str, repo: Path) -> GitResult:
    return run_git_command(action, GIT_ACTIONS[action], repo)


def get_current_branch(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    branch = completed.stdout.strip()
    return branch or "HEAD"


def get_git_dir(repo: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None

    git_dir = Path(completed.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    return git_dir.resolve()


def rebase_state_exists(repo: Path) -> bool:
    git_dir = get_git_dir(repo)
    if git_dir is None:
        return False
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def classify_failure(result: GitResult) -> RecoveryAnalysis:
    output = result.combined_output.lower()

    if result.has_merge_conflict or "unmerged paths" in output:
        return RecoveryAnalysis(
            category=MERGE_CONFLICT,
            explanation=(
                "Git found real file conflicts. Conflict Pilot will not choose a version "
                "or edit conflicted files automatically."
            ),
        )

    if rebase_state_exists(result.repo) or "rebase in progress" in output:
        return RecoveryAnalysis(
            category=REBASE_IN_PROGRESS,
            explanation=(
                "A rebase is already in progress. Resolve files manually, then use "
                "`git rebase --continue` or abort intentionally."
            ),
        )

    if "has no upstream branch" in output:
        branch = get_current_branch(result.repo)
        return RecoveryAnalysis(
            category=NO_UPSTREAM_BRANCH,
            explanation="This branch has no upstream remote branch yet.",
            safe_command=["git", "push", "--set-upstream", "origin", branch],
        )

    push_rejected_markers = (
        "failed to push some refs",
        "fetch first",
        "non-fast-forward",
        "updates were rejected",
        "tip of your current branch is behind",
    )
    if result.action == "push" and any(marker in output for marker in push_rejected_markers):
        return RecoveryAnalysis(
            category=PUSH_REJECTED_REMOTE_CHANGES,
            explanation=(
                "The remote branch has commits you do not have locally. The safe next "
                "step is to rebase your local work on top of the remote branch."
            ),
            safe_command=["git", "pull", "--rebase"],
        )

    uncommitted_markers = (
        "your local changes to the following files would be overwritten",
        "please commit your changes or stash them before you merge",
        "cannot pull with rebase: you have unstaged changes",
        "cannot rebase: you have unstaged changes",
    )
    if any(marker in output for marker in uncommitted_markers):
        return RecoveryAnalysis(
            category=UNCOMMITTED_CHANGES_BLOCKING_PULL,
            explanation=(
                "Local uncommitted changes are blocking the Git operation. Commit, "
                "stash, or discard those changes deliberately before retrying."
            ),
        )

    return RecoveryAnalysis(
        category=UNKNOWN_FAILURE,
        explanation="Conflict Pilot does not recognize this Git failure yet.",
    )


def play_result_feedback(result: GitResult, haptics: SharedSerialHaptics) -> None:
    if result.succeeded and result.action == "push":
        haptics.push_success()
    elif result.succeeded:
        haptics.success()
    elif result.has_merge_conflict:
        haptics.merge_conflict()
    else:
        haptics.failure()


def print_analysis(analysis: RecoveryAnalysis) -> None:
    print("\n--- Conflict Pilot ---")
    print(f"Detected: {analysis.category}")
    print(analysis.explanation)

    if analysis.safe_command:
        print("\nSuggested safe recovery command:")
        print(" ".join(analysis.safe_command))
        print("\nSingle tap: approve recovery")
    else:
        print("\nNo safe automatic recovery is available for this case.")
        print("Single tap: reprint this explanation")

    print("Double tap: reprint this explanation")
    print("Long press: retry original command")


def print_ai_git_help(result: GitResult, analysis: RecoveryAnalysis, enabled: bool) -> None:
    if not enabled:
        return

    print("\nAsking Groq to explain the Git failure...")
    try:
        help_text = explain_git_failure(
            repo=result.repo,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            category=analysis.category,
            safe_command=analysis.safe_command,
        )
    except Exception as error:
        print(f"Groq Git help failed: {error}")
        return

    print_git_help(help_text)


def wait_for_gesture(board: serial.Serial, gesture_config: GestureConfig) -> str:
    press_started_at: float | None = None
    long_press_fired = False
    pending_single_tap_at: float | None = None

    while True:
        line = board.readline().decode("utf-8", errors="replace").strip()
        now = time.monotonic()

        if pending_single_tap_at is not None:
            elapsed = now - pending_single_tap_at
            if elapsed >= gesture_config.double_tap_window_seconds:
                return "tap"

        if press_started_at is not None and not long_press_fired:
            press_duration = now - press_started_at
            if press_duration >= gesture_config.long_press_seconds:
                print(f"Touch duration: {press_duration:.2f}s")
                long_press_fired = True
                return "long"

        if not line:
            continue

        print(line)

        if TOUCH_PRESSED_PATTERN.search(line):
            press_started_at = now
            long_press_fired = False
            continue

        if TOUCH_RELEASED_PATTERN.search(line) and press_started_at is None:
            print("Touch duration: release-only")
            if pending_single_tap_at is not None:
                return "double"
            pending_single_tap_at = now
            continue

        if TOUCH_RELEASED_PATTERN.search(line) and press_started_at is not None:
            press_duration = now - press_started_at
            press_started_at = None
            print(f"Touch duration: {press_duration:.2f}s")

            if long_press_fired:
                return "long"
            if pending_single_tap_at is not None:
                return "double"
            pending_single_tap_at = now


def handle_failure_loop(
    original_action: str,
    repo: Path,
    initial_result: GitResult,
    board: serial.Serial,
    haptics: SharedSerialHaptics,
    gesture_config: GestureConfig,
    ai_help: bool,
) -> int:
    result = initial_result
    analysis = classify_failure(result)
    print_analysis(analysis)
    print_ai_git_help(result, analysis, ai_help)

    while True:
        gesture = wait_for_gesture(board, gesture_config)

        if gesture == "double":
            print_analysis(analysis)
            print_ai_git_help(result, analysis, ai_help)
            continue

        if gesture == "tap":
            if not analysis.safe_command:
                print_analysis(analysis)
                continue

            recovery_result = run_git_command("recovery", analysis.safe_command, repo)
            print_git_result(recovery_result)
            play_result_feedback(recovery_result, haptics)

            if recovery_result.succeeded:
                print(
                    "\nRecovery command succeeded. Long press to retry the original "
                    f"`git {original_action}` command."
                )
                analysis = RecoveryAnalysis(
                    category="RECOVERY_SUCCEEDED",
                    explanation=(
                        "The approved recovery command completed. Conflict Pilot will "
                        "not auto-run the original command."
                    ),
                )
            else:
                analysis = classify_failure(recovery_result)
                print_analysis(analysis)
                print_ai_git_help(recovery_result, analysis, ai_help)
            continue

        if gesture == "long":
            result = run_git_action(original_action, repo)
            print_git_result(result)
            play_result_feedback(result, haptics)
            if result.succeeded:
                return 0
            analysis = classify_failure(result)
            print_analysis(analysis)
            print_ai_git_help(result, analysis, ai_help)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tap-approved Git failure recovery for the FRDM-MCXN947."
    )
    parser.add_argument("action", choices=sorted(GIT_ACTIONS), help="Git action to run first.")
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
        "--long-press-seconds",
        default=0.35,
        type=float,
        help="Press duration that retries the original command.",
    )
    parser.add_argument(
        "--double-tap-window-seconds",
        default=0.25,
        type=float,
        help="Maximum gap between taps that reprints the explanation.",
    )
    parser.add_argument(
        "--ai-help",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Ask Groq to explain failed pull/push operations and safe next steps.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = args.repo.resolve()
    if not repo.exists() or not repo.is_dir():
        print(f"Repository path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2

    controller_config = load_config(args.config)
    port = find_serial_port() if controller_config.serial_port == "auto" else controller_config.serial_port
    gesture_config = GestureConfig(
        long_press_seconds=args.long_press_seconds,
        double_tap_window_seconds=args.double_tap_window_seconds,
    )

    print(f"Opening {port} at {controller_config.baud_rate} baud.")
    print(f"Repo: {repo}")
    print("Conflict Pilot controls: tap=approve safe fix, double tap=explain, long press=retry.")
    print("Press Ctrl+C to stop.\n")

    try:
        with serial.Serial(
            port=port,
            baudrate=controller_config.baud_rate,
            timeout=0.02,
            write_timeout=controller_config.write_timeout_seconds,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as board:
            board.reset_input_buffer()
            board.reset_output_buffer()
            haptics = SharedSerialHaptics(
                board=board,
                command_delay_seconds=controller_config.command_delay_seconds,
                line_ending=controller_config.line_ending,
            )

            result = run_git_action(args.action, repo)
            print_git_result(result)
            play_result_feedback(result, haptics)

            if result.succeeded:
                return 0

            return handle_failure_loop(
                original_action=args.action,
                repo=repo,
                initial_result=result,
                board=board,
                haptics=haptics,
                gesture_config=gesture_config,
                ai_help=args.ai_help,
            )

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except serial.SerialException as error:
        print(f"Serial failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
