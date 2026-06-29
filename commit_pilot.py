import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial

from haptic_controller import find_serial_port, load_config
from touch_git_controller import (
    SharedSerialHaptics,
    TOUCH_PRESSED_PATTERN,
    TOUCH_RELEASED_PATTERN,
    auto_reset_board,
)


@dataclass
class GestureConfig:
    long_press_seconds: float = 0.6
    double_tap_window_seconds: float = 0.8


def run_git(command: list[str], repo: Path) -> subprocess.CompletedProcess[str]:
    print(f"\nAbout to run: {' '.join(command)}")
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    print(f"Exit code: {completed.returncode}")

    if completed.stdout.strip():
        print("\n--- stdout ---")
        print(completed.stdout.rstrip())

    if completed.stderr.strip():
        print("\n--- stderr ---")
        print(completed.stderr.rstrip())

    return completed


def changed_files(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def has_any_changes(repo: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


def basic_commit_message(files: list[str], fallback: str | None) -> str:
    if fallback:
        return fallback

    if not files:
        return "Update project files"

    if len(files) == 1:
        name = Path(files[0]).name
        if name.lower() == "readme.md":
            return "Update README"
        return f"Update {name}"

    if all(Path(file).suffix.lower() == ".java" for file in files):
        return "Update Java files"

    return "Update project files"


def status_action(repo: Path, haptics: SharedSerialHaptics) -> None:
    result = run_git(["git", "status"], repo)
    haptics.success() if result.returncode == 0 else haptics.failure()


def stage_action(repo: Path, haptics: SharedSerialHaptics) -> None:
    result = run_git(["git", "add", "-A"], repo)
    haptics.success() if result.returncode == 0 else haptics.failure()
    if result.returncode == 0:
        run_git(["git", "status", "--short"], repo)


def commit_and_push_action(repo: Path, haptics: SharedSerialHaptics, message: str | None) -> None:
    if not has_any_changes(repo):
        print("\nNo local changes to commit.")
        haptics.success()
        return

    stage = run_git(["git", "add", "-A"], repo)
    if stage.returncode != 0:
        haptics.failure()
        return

    files = changed_files(repo)
    if not files:
        print("\nNo staged changes to commit.")
        haptics.success()
        return

    commit_message = basic_commit_message(files, message)
    print(f"\nCommit message: {commit_message}")
    commit = run_git(["git", "commit", "-m", commit_message], repo)
    if commit.returncode != 0:
        haptics.failure()
        return

    push = run_git(["git", "push"], repo)
    haptics.push_success() if push.returncode == 0 else haptics.failure()


def count_extra_taps(board: serial.Serial, window_seconds: float) -> int:
    tap_count = 1
    deadline = time.monotonic() + window_seconds

    while time.monotonic() < deadline:
        line = board.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue

        print(line)
        if TOUCH_PRESSED_PATTERN.search(line):
            tap_count += 1
            deadline = time.monotonic() + window_seconds

    return tap_count


def wait_for_action(board: serial.Serial, gesture_config: GestureConfig) -> str:
    while True:
        line = board.readline().decode("utf-8", errors="replace").strip()

        if not line:
            continue

        print(line)

        if TOUCH_PRESSED_PATTERN.search(line):
            tap_count = count_extra_taps(board, gesture_config.double_tap_window_seconds)
            print(f"Touch taps: {tap_count}")
            if tap_count >= 2:
                return "stage"
            return "status"

        if TOUCH_RELEASED_PATTERN.search(line):
            print("Touch duration: release-only")
            return "status"


def has_staged_changes(repo: Path) -> bool:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Touch-controlled basic commit and push workflow without Groq."
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
        "--message",
        help="Optional fixed commit message. Defaults to a basic file-based message.",
    )
    parser.add_argument(
        "--long-press-seconds",
        default=0.6,
        type=float,
        help="Reserved for long-press tuning. Triple tap is the reliable commit/push gesture.",
    )
    parser.add_argument(
        "--double-tap-window-seconds",
        default=0.8,
        type=float,
        help="Maximum gap between taps that stages all changes.",
    )
    parser.add_argument(
        "--no-auto-reset",
        action="store_true",
        help="Do not try to reset the board automatically after opening the serial port.",
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
    print("Commit Pilot controls: single tap=status, double tap=stage all, staged+single tap=commit and push.")
    print(f"Tap window: {gesture_config.double_tap_window_seconds:.2f}s")
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
            if args.no_auto_reset:
                print("Auto-reset disabled. If touches do not register, press RESET while this script is running.\n")
            else:
                auto_reset_board(board)
            print("Ready for touch input.\n")

            while True:
                action = wait_for_action(board, gesture_config)
                if action == "status" and has_staged_changes(repo):
                    action = "commit-push"
                    print("Staged changes detected, treating single tap as commit-push.")

                print(f"Gesture detected: {action}")
                if action == "status":
                    status_action(repo, haptics)
                elif action == "stage":
                    stage_action(repo, haptics)
                elif action == "commit-push":
                    commit_and_push_action(repo, haptics, args.message)
                print("\nReady for next touch.\n")

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except serial.SerialException as error:
        print(f"Serial failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
