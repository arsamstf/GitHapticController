import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial

from git_haptic import GitResult, print_git_result, run_git_action
from haptic_controller import find_serial_port, load_config


TOUCH_PRESSED_PATTERN = re.compile(r"\binput touch pressed\b")
TOUCH_RELEASED_PATTERN = re.compile(r"\binput touch released\b")
TOUCH_READY_PATTERN = re.compile(r"\binput touch ready\b")
TOUCH_EVENT_PATTERN = re.compile(r"\bevent touch (single|double|long)\b")
TOUCH_EVENT_READY_PATTERN = re.compile(r"\bevent touch ready\b")


@dataclass
class TouchGitConfig:
    repo: Path
    long_press_seconds: float = 0.6
    double_tap_window_seconds: float = 0.7


class SharedSerialHaptics:
    def __init__(self, board: serial.Serial, command_delay_seconds: float, line_ending: str, show_echoes: bool = True):
        self.board = board
        self.command_delay_seconds = command_delay_seconds
        self.line_ending = line_ending
        self.show_echoes = show_echoes

    def send_raw(self, command: str) -> None:
        endings = {
            "crlf": "\r\n",
            "lf": "\n",
            "cr": "\r",
            "none": "",
        }
        ending = endings.get(self.line_ending)
        if ending is None:
            raise ValueError(f"Unsupported line ending: {self.line_ending}")

        self.board.write(f"{command}{ending}".encode("ascii"))
        self.board.flush()
        time.sleep(self.command_delay_seconds)

    def send(self, command: str) -> None:
        self.send_raw(command)

    def drain_echoes(self, seconds: float = 0.25) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = self.board.readline().decode("utf-8", errors="replace").strip()
            if line and self.show_echoes:
                print(line)

    def balanced_pulse(self, seconds: float = 0.25) -> None:
        self.send("bal on")
        time.sleep(seconds)
        self.send("bal off")
        self.drain_echoes()

    def unbalanced_pulse(self, seconds: float = 0.25) -> None:
        self.send("unb on")
        time.sleep(seconds)
        self.send("unb off")
        self.drain_echoes()

    def success(self) -> None:
        self.balanced_pulse(0.2)

    def push_success(self) -> None:
        self.balanced_pulse(0.8)

    def failure(self) -> None:
        for _ in range(3):
            self.unbalanced_pulse(0.12)
            time.sleep(0.08)

    def merge_conflict(self) -> None:
        for _ in range(5):
            self.unbalanced_pulse(0.2)
            time.sleep(0.2)


def play_feedback(result: GitResult, haptics: SharedSerialHaptics) -> None:
    if result.succeeded and result.action == "push":
        haptics.push_success()
    elif result.succeeded:
        haptics.success()
    elif result.has_merge_conflict:
        haptics.merge_conflict()
    else:
        haptics.failure()


def run_action(action: str, repo: Path, haptics: SharedSerialHaptics) -> None:
    print(f"\nTouch action: {action}")
    result = run_git_action(action, repo)
    print_git_result(result)
    play_feedback(result, haptics)
    print("Ready for next touch.\n")


def run_git_command(action: str, command: list[str], repo: Path) -> GitResult:
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    return GitResult(
        action=action,
        command=command,
        repo=repo,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def has_any_changes(repo: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


def changed_files(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def basic_commit_message(files: list[str]) -> str:
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


def commit_and_push(repo: Path, haptics: SharedSerialHaptics) -> None:
    print("\nTouch action: commit and push")

    if has_any_changes(repo):
        stage = run_git_command("stage", ["git", "add", "-A"], repo)
        print_git_result(stage)
        if not stage.succeeded:
            play_feedback(stage, haptics)
            print("Ready for next touch.\n")
            return

        files = changed_files(repo)
        if files:
            message = basic_commit_message(files)
            print(f"\nCommit message: {message}")
            commit = run_git_command("commit", ["git", "commit", "-m", message], repo)
            print_git_result(commit)
            if not commit.succeeded:
                play_feedback(commit, haptics)
                print("Ready for next touch.\n")
                return

    push = run_git_command("push", ["git", "push"], repo)
    print_git_result(push)
    play_feedback(push, haptics)
    print("Ready for next touch.\n")


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


def wait_for_touch_ready(board: serial.Serial, seconds: float = 3.0) -> bool:
    print("Waiting briefly for touch firmware.")
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        line = board.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue

        print(line)
        if TOUCH_READY_PATTERN.search(line) or TOUCH_EVENT_READY_PATTERN.search(line):
            print("Touch firmware ready.\n")
            return True

    return False


def auto_reset_board(board: serial.Serial) -> None:
    print("Auto-resetting board through serial control lines.")
    reset_attempts = [
        ((False, False), (True, True)),
        ((True, True), (False, False)),
        ((False, True), (True, False)),
        ((True, False), (False, True)),
    ]

    for attempt_number, (assert_state, release_state) in enumerate(reset_attempts, start=1):
        print(f"Reset attempt {attempt_number}.")
        board.reset_input_buffer()
        board.reset_output_buffer()

        board.dtr, board.rts = assert_state
        time.sleep(0.2)
        board.dtr, board.rts = release_state
        time.sleep(0.2)

        if wait_for_touch_ready(board, seconds=2.5):
            return

    try:
        print("Trying serial break reset pulse.")
        board.reset_input_buffer()
        board.send_break(duration=1)
        if wait_for_touch_ready(board, seconds=2.5):
            return
    except serial.SerialException:
        pass

    print("Auto-reset did not show a touch-ready message. Continuing anyway.\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Listen for touch events and run Git commands with haptic feedback."
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
        "--long-press-seconds",
        default=0.6,
        type=float,
        help="Reserved for long-press tuning. Triple tap is the reliable push gesture.",
    )
    parser.add_argument(
        "--double-tap-window-seconds",
        default=0.7,
        type=float,
        help="Maximum gap between taps that maps to git pull.",
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
    touch_config = TouchGitConfig(
        repo=repo,
        long_press_seconds=args.long_press_seconds,
        double_tap_window_seconds=args.double_tap_window_seconds,
    )

    print(f"Opening {port} at {controller_config.baud_rate} baud.")
    print(f"Repo: {repo}")
    print("Touch controls: single tap=status, double tap=pull, long touch=stage, commit, push.")
    print("With old firmware fallback: triple tap=stage, commit, push.")
    print(f"Tap window: {touch_config.double_tap_window_seconds:.2f}s")
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
                line = board.readline().decode("utf-8", errors="replace").strip()

                if not line:
                    continue

                print(line)

                touch_event = TOUCH_EVENT_PATTERN.search(line)
                if touch_event:
                    event = touch_event.group(1)
                    if event == "single":
                        run_action("status", repo, haptics)
                    elif event == "double":
                        run_action("pull", repo, haptics)
                    elif event == "long":
                        commit_and_push(repo, haptics)
                    continue

                if TOUCH_PRESSED_PATTERN.search(line):
                    tap_count = count_extra_taps(board, touch_config.double_tap_window_seconds)
                    print(f"Touch taps: {tap_count}")
                    if tap_count >= 3:
                        commit_and_push(repo, haptics)
                    elif tap_count == 2:
                        run_action("pull", repo, haptics)
                    else:
                        run_action("status", repo, haptics)
                    continue

                if TOUCH_RELEASED_PATTERN.search(line):
                    print("Touch duration: release-only")
                    run_action("status", repo, haptics)
                    continue

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except serial.SerialException as error:
        print(f"Serial failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
