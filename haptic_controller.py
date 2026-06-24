import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import serial
from serial.tools import list_ports


VALID_COMMANDS = {
    "bal on",
    "bal off",
    "unb on",
    "unb off",
}


@dataclass
class HapticConfig:
    serial_port: str = "auto"
    baud_rate: int = 115200
    command_delay_seconds: float = 0.05
    write_timeout_seconds: float = 5.0
    line_ending: str = "crlf"


def load_config(path: Path) -> HapticConfig:
    if not path.exists():
        return HapticConfig()

    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    return HapticConfig(
        serial_port=data.get("serial_port", "auto"),
        baud_rate=int(data.get("baud_rate", 115200)),
        command_delay_seconds=float(data.get("command_delay_seconds", 0.05)),
        write_timeout_seconds=float(data.get("write_timeout_seconds", 5)),
        line_ending=data.get("line_ending", "crlf"),
    )


def available_ports() -> list[str]:
    ports = []
    for port in list_ports.comports():
        label = f"{port.device}"
        details = []
        if port.description:
            details.append(port.description)
        if port.manufacturer:
            details.append(port.manufacturer)
        if details:
            label += f" - {' | '.join(details)}"
        ports.append(label)
    return ports


def find_serial_port() -> str:
    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found. Is the FRDM-MCXN947 plugged in?")

    keywords = ("nxp", "cmsis", "dap", "mbed", "jlink", "serial", "usb")
    for port in ports:
        haystack = " ".join(
            value
            for value in (port.description, port.manufacturer, port.product, port.hwid)
            if value
        ).lower()
        if any(keyword in haystack for keyword in keywords):
            return port.device

    if len(ports) == 1:
        return ports[0].device

    choices = "\n".join(f"  {port.device}: {port.description}" for port in ports)
    raise RuntimeError(
        "Could not auto-select a serial port. Set serial_port in config.json.\n"
        f"Available ports:\n{choices}"
    )


class HapticController:
    def __init__(self, config: HapticConfig):
        self.config = config
        self.port = find_serial_port() if config.serial_port == "auto" else config.serial_port
        self.serial = serial.Serial(
            port=self.port,
            baudrate=config.baud_rate,
            timeout=1,
            write_timeout=config.write_timeout_seconds,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()
        time.sleep(0.2)

    def close(self) -> None:
        if self.serial.is_open:
            self.serial.close()

    def send(self, command: str) -> None:
        if command not in VALID_COMMANDS:
            valid = ", ".join(sorted(VALID_COMMANDS))
            raise ValueError(f"Unsupported command: {command!r}. Valid commands: {valid}")

        self.send_raw(command)

    def send_raw(self, command: str) -> None:
        endings = {
            "crlf": "\r\n",
            "lf": "\n",
            "cr": "\r",
            "none": "",
        }
        if self.config.line_ending not in endings:
            valid = ", ".join(sorted(endings))
            raise ValueError(f"Unsupported line_ending: {self.config.line_ending!r}. Use: {valid}")

        self.serial.write(f"{command}{endings[self.config.line_ending]}".encode("ascii"))
        self.serial.flush()
        time.sleep(self.config.command_delay_seconds)

    def motor_off(self) -> None:
        self.send("bal off")
        self.send("unb off")

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

    def __enter__(self) -> "HapticController":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send motor commands to the FRDM-MCXN947.")
    parser.add_argument(
        "action",
        choices=[
            "list-ports",
            "bal-on",
            "bal-off",
            "unb-on",
            "unb-off",
            "off",
            "success",
            "push-success",
            "failure",
            "merge-conflict",
            "raw",
        ],
        help="Command or haptic pattern to run.",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to config.json.",
    )
    parser.add_argument(
        "--text",
        help="Text to send when action is raw, for example: --text \"bal on\"",
    )
    parser.add_argument(
        "--seconds",
        default=0.25,
        type=float,
        help="Pulse length for direct pulse-style actions.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.action == "list-ports":
        ports = available_ports()
        if not ports:
            print("No serial ports found.")
            return 1
        print("\n".join(ports))
        return 0

    config = load_config(args.config)

    command_map = {
        "bal-on": "bal on",
        "bal-off": "bal off",
        "unb-on": "unb on",
        "unb-off": "unb off",
    }

    with HapticController(config) as controller:
        print(f"Connected to {controller.port} at {config.baud_rate} baud.")
        try:
            if args.action in command_map:
                controller.send(command_map[args.action])
            elif args.action == "off":
                controller.motor_off()
            elif args.action == "success":
                controller.balanced_pulse(args.seconds)
            elif args.action == "push-success":
                controller.push_success()
            elif args.action == "failure":
                controller.failure()
            elif args.action == "merge-conflict":
                controller.merge_conflict()
            elif args.action == "raw":
                if not args.text:
                    parser.error('raw requires --text, for example: raw --text "bal on"')
                controller.send_raw(args.text)
        except serial.SerialTimeoutException:
            print(
                "Serial write timed out. Close any serial terminal using the board, "
                "press reset on the FRDM-MCXN947, then try again. If it still fails, "
                "check that the UART shell firmware is running on the VCom port."
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

