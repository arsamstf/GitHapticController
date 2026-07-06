import os
import subprocess
import time
from pathlib import Path

from serial.tools import list_ports


APP_DIR = Path(__file__).resolve().parent
APP_LAUNCHER = APP_DIR / "Git Haptic Controller.vbs"
WATCHER_PID_FILE = APP_DIR / ".git_haptic_watcher.pid"
POLL_SECONDS = 2.0

BOARD_KEYWORDS = (
    "mcu-link",
    "nxp",
    "vcom",
    "cmsis",
    "frdm",
)


def board_ports() -> list[str]:
    matches = []
    for port in list_ports.comports():
        haystack = " ".join(
            value
            for value in (port.device, port.description, port.manufacturer, port.product, port.hwid)
            if value
        ).lower()
        if any(keyword in haystack for keyword in BOARD_KEYWORDS):
            matches.append(port.device)
    return matches


def launch_app() -> None:
    if os.name == "nt":
        env = os.environ.copy()
        env["GIT_HAPTIC_SILENT_DUPLICATE"] = "1"
        subprocess.Popen(
            ["wscript.exe", str(APP_LAUNCHER)],
            cwd=APP_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        python = APP_DIR / ".venv" / "Scripts" / "python"
        subprocess.Popen([str(python), str(APP_DIR / "git_haptic_app.py")], cwd=APP_DIR)


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def cleanup_duplicate_helpers() -> None:
    if os.name != "nt":
        return
    app_pid = read_pid(APP_DIR / ".git_haptic_app.pid")
    watcher_pid = read_pid(WATCHER_PID_FILE)
    keep = ",".join(f"'{pid}'" for pid in (app_pid, watcher_pid) if pid > 0)
    if not keep:
        return
    command = (
        f"$keep=@({keep}); "
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "($_.Name -eq 'pythonw.exe') -and "
        "($_.CommandLine -like '*device_watcher.py*' -or $_.CommandLine -like '*git_haptic_app.py*') -and "
        "($keep -notcontains ([string]$_.ProcessId)) "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=APP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def is_pid_running(pid: int) -> bool:
    if pid <= 0 or os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return str(pid) in completed.stdout


def acquire_pid_file(path: Path) -> bool:
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing_pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                existing_pid = 0
            if is_pid_running(existing_pid):
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        return True
    return False


def main() -> int:
    if not acquire_pid_file(WATCHER_PID_FILE):
        os._exit(0)

    previously_connected = bool(board_ports())
    if previously_connected:
        launch_app()

    try:
        while True:
            connected = bool(board_ports())
            if connected and not previously_connected:
                launch_app()
            previously_connected = connected
            time.sleep(POLL_SECONDS)
    finally:
        try:
            if WATCHER_PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                WATCHER_PID_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
