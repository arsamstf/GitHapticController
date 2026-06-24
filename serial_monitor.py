import argparse
import time
from pathlib import Path

import serial

from haptic_controller import find_serial_port, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print serial output from the FRDM-MCXN947.")
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

    print(f"Listening on {port} at {config.baud_rate} baud. Press Ctrl+C to stop.")
    print("Touch/press the board input now. Incoming serial text will appear below.\n")

    try:
        with serial.Serial(
            port=port,
            baudrate=config.baud_rate,
            timeout=0.2,
            write_timeout=config.write_timeout_seconds,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as board:
            board.reset_input_buffer()
            while deadline is None or time.monotonic() < deadline:
                data = board.readline()
                if data:
                    print(data.decode("utf-8", errors="replace").rstrip())
    except KeyboardInterrupt:
        print("\nStopped.")
    except serial.SerialException as error:
        print(f"Serial monitor failed: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
