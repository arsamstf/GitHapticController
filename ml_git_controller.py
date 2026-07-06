import argparse
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import serial

from git_haptic import GitResult, print_git_result, run_git_action
from groq_review import explain_git_failure, print_git_help, print_review, review_repo
from haptic_controller import find_serial_port, load_config
from touch_git_controller import SharedSerialHaptics, play_feedback


PREDICTION_PATTERN = re.compile(
    r"RESULT\s+\[\s*(?P<scores>[^\]]+)\]\s+CLASS:\s*(?P<class_id>\d+)"
)

CLASS_NAMES = {
    0: "Idle",
    1: "1Tap / Status",
    2: "ShakeCancel",
    3: "TiltForward / Pull",
}

CLASS_ACTIONS = {
    1: "status",
    3: "pull",
}


@dataclass
class Prediction:
    class_id: int
    confidence: float
    line: str

    @property
    def label(self) -> str:
        return CLASS_NAMES.get(self.class_id, f"Unknown class {self.class_id}")


@dataclass
class PendingPush:
    expires_at: float

    def active(self) -> bool:
        return time.monotonic() < self.expires_at


@dataclass
class MotorState:
    enabled: bool = True

    def haptics(self, haptics: SharedSerialHaptics | None) -> SharedSerialHaptics | None:
        if not self.enabled:
            return None
        return haptics

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        return self.enabled


def parse_prediction(line: str) -> Prediction | None:
    match = PREDICTION_PATTERN.search(line)
    if not match:
        return None

    class_id = int(match.group("class_id"))
    scores = [float(value) for value in match.group("scores").split()]
    confidence = scores[class_id] if 0 <= class_id < len(scores) else 0.0
    return Prediction(class_id=class_id, confidence=confidence, line=line)


def send_shell_command(board: serial.Serial, command: str, line_ending: str, delay_seconds: float = 0.15) -> None:
    endings = {
        "crlf": "\r\n",
        "lf": "\n",
        "cr": "\r",
        "none": "",
    }
    ending = endings.get(line_ending)
    if ending is None:
        raise ValueError(f"Unsupported line ending: {line_ending}")

    board.write(f"{command}{ending}".encode("ascii"))
    board.flush()
    time.sleep(delay_seconds)


def drain_lines(board: serial.Serial, seconds: float, debug: bool) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = board.readline().decode("utf-8", errors="replace").strip()
        if line and debug:
            print(line)


def start_prediction_mode(board: serial.Serial, line_ending: str, debug: bool) -> None:
    commands = [
        "stop",
        "setup sensor 0",
        "setup sc 128",
        "setup odr 100",
        "setup fsr 2",
        "start pre",
    ]
    for command in commands:
        if debug:
            print(f"> {command}")
        send_shell_command(board, command, line_ending)
        drain_lines(board, seconds=0.35, debug=debug)


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
        print(f"Groq Git help failed: {error}")
        return

    print_git_help(help_text)


def run_action(
    action: str,
    repo: Path,
    haptics: SharedSerialHaptics | None,
    ai_help: bool,
) -> GitResult:
    print(f"\nGesture action: git {action}")
    result = run_git_action(action, repo)
    print_git_result(result)

    if haptics is not None:
        play_feedback(result, haptics)

    print_ai_git_help(result, ai_help)
    print("\nListening for gestures.\n")
    return result


def stable_prediction(
    history: deque[Prediction],
    stable_count: int,
    confidence_threshold: float,
    status_stable_count: int,
    status_confidence_threshold: float,
    shake_stable_count: int,
    shake_confidence_threshold: float,
    push_confirm_active: bool,
    push_confirm_stable_count: int,
    push_confirm_confidence_threshold: float,
) -> Prediction | None:
    if not history:
        return None

    latest_class = history[-1].class_id
    if latest_class == 1 and push_confirm_active:
        required_count = push_confirm_stable_count
        required_confidence = push_confirm_confidence_threshold
    elif latest_class == 1:
        required_count = status_stable_count
        required_confidence = status_confidence_threshold
    elif latest_class == 2:
        required_count = shake_stable_count
        required_confidence = shake_confidence_threshold
    else:
        required_count = stable_count
        required_confidence = confidence_threshold

    if len(history) < required_count:
        return None

    window = list(history)[-required_count:]
    first_class = window[0].class_id
    if any(prediction.class_id != first_class for prediction in window):
        return None
    if any(prediction.confidence < required_confidence for prediction in window):
        return None
    return window[-1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Git commands from deployed Time Series Studio prediction classes."
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
        "--no-setup",
        action="store_true",
        help="Do not send setup/start pre commands. Use this if prediction is already running.",
    )
    parser.add_argument(
        "--no-haptic",
        action="store_true",
        help="Do not send motor feedback commands after Git actions.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw serial prediction lines.",
    )
    parser.add_argument(
        "--stable-count",
        default=2,
        type=int,
        help="Number of same predictions required before triggering an action.",
    )
    parser.add_argument(
        "--confidence",
        default=0.80,
        type=float,
        help="Minimum class confidence required before triggering an action.",
    )
    parser.add_argument(
        "--status-confidence",
        default=0.88,
        type=float,
        help="Minimum confidence required before git status triggers.",
    )
    parser.add_argument(
        "--status-stable-count",
        default=1,
        type=int,
        help="Number of same status predictions required before git status triggers.",
    )
    parser.add_argument(
        "--cooldown",
        default=2.0,
        type=float,
        help="Seconds to ignore repeated actions after a gesture triggers.",
    )
    parser.add_argument(
        "--idle-confidence",
        default=0.70,
        type=float,
        help="Minimum idle confidence required before the next gesture can trigger.",
    )
    parser.add_argument(
        "--status-repeat-seconds",
        default=5.0,
        type=float,
        help="Minimum seconds between repeated git status actions.",
    )
    parser.add_argument(
        "--shake-confidence",
        default=0.65,
        type=float,
        help="Minimum confidence required before ShakeCancel triggers.",
    )
    parser.add_argument(
        "--shake-stable-count",
        default=1,
        type=int,
        help="Number of same shake predictions required before ShakeCancel triggers.",
    )
    parser.add_argument(
        "--push-confirm-window",
        default=10.0,
        type=float,
        help="Seconds after a successful pull where 1Tap confirms git push and ShakeCancel cancels.",
    )
    parser.add_argument(
        "--push-confirm-confidence",
        default=0.70,
        type=float,
        help="Minimum 1Tap confidence required to confirm a pending push.",
    )
    parser.add_argument(
        "--push-confirm-stable-count",
        default=1,
        type=int,
        help="Number of same 1Tap predictions required to confirm a pending push.",
    )
    parser.add_argument(
        "--ai-review",
        action="store_true",
        help="Run a Groq diff review before a tap-confirmed push.",
    )
    parser.add_argument(
        "--ai-block-high-risk",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Block tap-confirmed push when Groq reports high risk.",
    )
    parser.add_argument(
        "--ai-help",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Ask Groq to explain failed pull/push operations.",
    )
    return parser


def review_before_push(repo: Path, haptics: SharedSerialHaptics | None, block_high_risk: bool) -> bool:
    print("\nRunning Groq review before push...")
    try:
        review = review_repo(repo)
    except Exception as error:
        print(f"Groq review failed: {error}")
        print("Push blocked because AI review did not complete.")
        if haptics is not None:
            haptics.failure()
        return False

    print_review(review)
    if review.has_no_diff:
        print("\nPush canceled: there are no staged or unstaged code changes to review.")
        print("Stage or edit files first, then run the pull/push confirmation flow again.")
        print("Manual stage command if needed: git add -A")
        if haptics is not None:
            haptics.failure()
        return False

    if review.is_high_risk and block_high_risk:
        print("\nPush blocked by Groq review. Fix or inspect the issue, then try again.")
        if haptics is not None:
            haptics.failure()
        return False

    print("\nGroq review did not block the push.")
    return True


def main() -> int:
    args = build_parser().parse_args()
    repo = args.repo.resolve()
    if not repo.exists() or not repo.is_dir():
        print(f"Repository path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2

    config = load_config(args.config)
    port = find_serial_port() if config.serial_port == "auto" else config.serial_port

    print(f"Opening {port} at {config.baud_rate} baud.")
    print(f"Repo: {repo}")
    print("ML gesture controls:")
    print("  CLASS 0 / Idle               -> no action")
    print("  CLASS 1 / 1Tap               -> git status, or confirm push if pending")
    print("  CLASS 2 / ShakeCancel        -> cancel pending push, or toggle motors")
    print("  CLASS 3 / TiltForward/Pull   -> git pull, then request push approval")
    print(f"Push confirmation window: {args.push_confirm_window:.1f}s")
    print("Press Ctrl+C to stop.\n")

    try:
        with serial.Serial(
            port=port,
            baudrate=config.baud_rate,
            timeout=0.05,
            write_timeout=config.write_timeout_seconds,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as board:
            board.reset_input_buffer()
            board.reset_output_buffer()

            if not args.no_setup:
                start_prediction_mode(board, config.line_ending, args.debug)

            haptics = None
            if not args.no_haptic:
                haptics = SharedSerialHaptics(
                    board=board,
                    command_delay_seconds=config.command_delay_seconds,
                    line_ending=config.line_ending,
                    show_echoes=args.debug,
                )

            history_size = max(
                args.stable_count,
                args.status_stable_count,
                args.shake_stable_count,
                args.push_confirm_stable_count,
                1,
            )
            history: deque[Prediction] = deque(maxlen=history_size)
            last_trigger_time = 0.0
            last_action_class: int | None = None
            pending_push: PendingPush | None = None
            motor_state = MotorState(enabled=not args.no_haptic)
            gesture_armed = True
            last_status_time = 0.0

            print("Listening for ML predictions.\n")
            while True:
                line = board.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if args.debug:
                    print(line)

                prediction = parse_prediction(line)
                if prediction is None:
                    continue

                history.append(prediction)
                stable = stable_prediction(
                    history,
                    args.stable_count,
                    args.confidence,
                    args.status_stable_count,
                    args.status_confidence,
                    args.shake_stable_count,
                    args.shake_confidence,
                    bool(pending_push and pending_push.active()),
                    args.push_confirm_stable_count,
                    args.push_confirm_confidence,
                )
                if stable is None:
                    continue

                now = time.monotonic()

                if stable.class_id == 0:
                    if stable.confidence >= args.idle_confidence:
                        gesture_armed = True
                    continue

                if pending_push and not pending_push.active():
                    pending_push = None
                    print("Push approval expired. No push will run.\n")

                if stable.class_id == 2:
                    print(f"Gesture detected: {stable.label} ({stable.confidence:.2f})")
                    if pending_push and pending_push.active():
                        pending_push = None
                        print("Pending push canceled.\n")
                    else:
                        enabled = motor_state.toggle()
                        print(f"Motors {'enabled' if enabled else 'disabled'}.\n")
                        active_haptics = motor_state.haptics(haptics)
                        if active_haptics is not None:
                            active_haptics.success()
                    last_trigger_time = now
                    last_action_class = stable.class_id
                    gesture_armed = False
                    history.clear()
                    continue

                if stable.class_id == 1 and pending_push and pending_push.active():
                    print(f"Gesture detected: {stable.label} ({stable.confidence:.2f})")
                    pending_push = None
                    active_haptics = motor_state.haptics(haptics)
                    if args.ai_review and not review_before_push(repo, active_haptics, args.ai_block_high_risk):
                        last_trigger_time = time.monotonic()
                        last_action_class = stable.class_id
                        gesture_armed = False
                        history.clear()
                        continue
                    run_action("push", repo, active_haptics, args.ai_help)
                    last_trigger_time = time.monotonic()
                    last_action_class = stable.class_id
                    gesture_armed = False
                    history.clear()
                    continue

                if not gesture_armed:
                    if args.debug:
                        print(
                            f"Ignoring {stable.label} ({stable.confidence:.2f}) "
                            "until idle is seen."
                    )
                    continue

                if (
                    stable.class_id == 1
                    and not (pending_push and pending_push.active())
                    and now - last_status_time < args.status_repeat_seconds
                ):
                    if args.debug:
                        print(
                            f"Ignoring repeated status ({stable.confidence:.2f}) "
                            f"for {args.status_repeat_seconds:.1f}s."
                        )
                    continue

                if now - last_trigger_time < args.cooldown and stable.class_id == last_action_class:
                    continue

                print(f"Gesture detected: {stable.label} ({stable.confidence:.2f})")
                gesture_armed = False

                action = CLASS_ACTIONS.get(stable.class_id)
                if action is None:
                    print(f"No action mapped for class {stable.class_id}.\n")
                    last_trigger_time = now
                    last_action_class = stable.class_id
                    continue

                result = run_action(action, repo, motor_state.haptics(haptics), args.ai_help)
                if action == "status":
                    last_status_time = time.monotonic()
                if stable.class_id == 3 and result.succeeded and args.push_confirm_window > 0:
                    pending_push = PendingPush(time.monotonic() + args.push_confirm_window)
                    print(
                        "Push pending: 1Tap confirms git push, "
                        "ShakeCancel cancels, or wait for timeout.\n"
                    )
                last_trigger_time = time.monotonic()
                last_action_class = stable.class_id
                history.clear()

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except serial.SerialException as error:
        print(f"Serial failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
