import argparse
import sys
import threading
import time
from pathlib import Path

import serial

from haptic_controller import find_serial_port, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive serial console for the FRDM-MCXN947.")
    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to haptic controller config.json.",
    )
    return parser


def reader(board: serial.Serial, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            data = board.readline()
        except serial.SerialException as error:
            if not stop_event.is_set():
                print(f"\nSerial reader stopped: {error}", file=sys.stderr)
                stop_event.set()
            return

        if data:
            print(data.decode("utf-8", errors="replace").rstrip())


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    port = find_serial_port() if config.serial_port == "auto" else config.serial_port

    print(f"Opening {port} at {config.baud_rate} baud.")
    print("Type shell commands like 'bal on'. Press Ctrl+C to stop.")
    print("Tip: press the board reset button after this opens to catch boot prints.\n")

    stop_event = threading.Event()

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
            thread = threading.Thread(target=reader, args=(board, stop_event), daemon=True)
            thread.start()

            while not stop_event.is_set():
                line = input()
                board.write(f"{line}\r\n".encode("ascii"))
                board.flush()
                time.sleep(config.command_delay_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
    except serial.SerialException as error:
        print(f"Serial console failed: {error}", file=sys.stderr)
        return 1
    finally:
        stop_event.set()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
