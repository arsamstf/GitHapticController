import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial

from git_haptic import GitResult, print_git_result, run_git_action
from haptic_controller import find_serial_port, load_config


TOUCH_PRESSED_PATTERN = re.compile(r"\binput touch pressed\b")
TOUCH_RELEASED_PATTERN = re.compile(r"\binput touch released\b")


@dataclass
class TouchGitConfig:
    repo: Path
    long_press_seconds: float = 0.8
    double_tap_window_seconds: float = 0.4


class SharedSerialHaptics:
    def __init__(self, board: serial.Serial, command_delay_seconds: float, line_ending: str):
        self.board = board
        self.command_delay_seconds = command_delay_seconds
        self.line_ending = line_ending

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

    def balanced_pulse(self, seconds: float = 0.25) -> None:
        self.send("bal on")
        time.sleep(seconds)
        self.send("bal off")

    def unbalanced_pulse(self, seconds: float = 0.25) -> None:
        self.send("unb on")
        time.sleep(seconds)
        self.send("unb off")

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
        default=0.8,
        type=float,
        help="Press duration that maps to git push.",
    )
    parser.add_argument(
        "--double-tap-window-seconds",
        default=0.4,
        type=float,
        help="Maximum gap between taps that maps to git pull.",
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
    print("Touch controls: single tap=status, double tap=pull, long press=push.")
    print("Press Ctrl+C to stop.\n")

    press_started_at: float | None = None
    pending_single_tap_at: float | None = None

    try:
        with serial.Serial(
            port=port,
            baudrate=controller_config.baud_rate,
            timeout=0.05,
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

            while True:
                line = board.readline().decode("utf-8", errors="replace").strip()
                now = time.monotonic()

                if pending_single_tap_at is not None:
                    elapsed = now - pending_single_tap_at
                    if elapsed >= touch_config.double_tap_window_seconds:
                        run_action("status", repo, haptics)
                        pending_single_tap_at = None

                if not line:
                    continue

                print(line)

                if TOUCH_PRESSED_PATTERN.search(line):
                    press_started_at = now
                    continue

                if TOUCH_RELEASED_PATTERN.search(line) and press_started_at is not None:
                    press_duration = now - press_started_at
                    press_started_at = None

                    if press_duration >= touch_config.long_press_seconds:
                        pending_single_tap_at = None
                        run_action("push", repo, haptics)
                    elif pending_single_tap_at is not None:
                        pending_single_tap_at = None
                        run_action("pull", repo, haptics)
                    else:
                        pending_single_tap_at = now

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except serial.SerialException as error:
        print(f"Serial failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
