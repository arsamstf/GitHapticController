import argparse
import time
from pathlib import Path

import serial

from haptic_controller import find_serial_port, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Timestamp raw touch events without Git or haptics.")
    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to haptic controller config.json.",
    )
    parser.add_argument(
        "--seconds",
        default=0,
        type=float,
        help="How long to listen. Use 0 to listen until Ctrl+C.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    port = find_serial_port() if config.serial_port == "auto" else config.serial_port
    deadline = None if args.seconds <= 0 else time.monotonic() + args.seconds
    start = time.monotonic()

    duration = "until Ctrl+C" if deadline is None else f"for {args.seconds:g}s"
    print(f"Listening on {port} {duration}.")
    print("Tap once, wait one second, then tap once again.\n")

    try:
        with serial.Serial(
            port=port,
            baudrate=config.baud_rate,
            timeout=0.02,
            write_timeout=config.write_timeout_seconds,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as board:
            board.reset_input_buffer()
            while deadline is None or time.monotonic() < deadline:
                line = board.readline().decode("utf-8", errors="replace").strip()
                if line:
                    elapsed = time.monotonic() - start
                    print(f"{elapsed:6.2f}s | {line}")
    except KeyboardInterrupt:
        print("\nStopped.")
    except serial.SerialException as error:
        print(f"Touch probe failed: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
