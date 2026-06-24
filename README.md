# Git Haptic Controller

Python control code for the Git Haptic Controller.

- Phase 1: send motor commands to the FRDM-MCXN947 over USB serial.
- Phase 2: run Git commands and play haptic feedback for success/failure.

## Setup

```powershell
cd C:\GitHapticController
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Check Serial Ports

```powershell
python haptic_controller.py list-ports
```

If auto-detection picks the wrong port, edit `config.json`:

```json
{
  "serial_port": "COM5",
  "baud_rate": 115200,
  "command_delay_seconds": 0.05
}
```

## Direct Motor Commands

```powershell
python haptic_controller.py bal-on
python haptic_controller.py bal-off
python haptic_controller.py unb-on
python haptic_controller.py unb-off
python haptic_controller.py off
```

## Feedback Patterns

```powershell
python haptic_controller.py success
python haptic_controller.py push-success
python haptic_controller.py failure
python haptic_controller.py merge-conflict
```

These patterns are the pieces Phase 2 will call after Git commands succeed or fail.

## Monitor Board Serial Output

Use this before mapping touch events to Git actions:

```powershell
python serial_monitor.py
```

Then touch/press the board input. If the firmware prints an event, it will show up in PowerShell.

Listen for 15 seconds:

```powershell
python serial_monitor.py --seconds 15
```

Use an interactive serial console:

```powershell
python serial_console.py
```

After it opens, press the board reset button to catch boot messages. You can also type shell commands such as `bal on` and `bal off`.

## Git Commands With Haptic Feedback

Close or disconnect the MCUXpresso serial terminal before running these commands, because only one app can use the board COM port at a time.

Run Git in the current directory:

```powershell
python git_haptic.py status
python git_haptic.py pull
python git_haptic.py push
```

Run Git in a specific repo:

```powershell
python git_haptic.py status --repo C:\path\to\repo
python git_haptic.py pull --repo C:\path\to\repo
python git_haptic.py push --repo C:\path\to\repo
```

Test Git output without motor feedback:

```powershell
python git_haptic.py status --repo C:\path\to\repo --no-haptic
```

Feedback patterns:

- `status` or `pull` success: balanced short pulse
- `push` success: balanced long pulse
- Git failure: unbalanced rapid pulse
- Merge conflict text detected: repeated unbalanced vibration

## Touch Git Controller

After the command-line Git flow works, run the touch listener. This script keeps one serial connection open so it can read touch events and send motor commands without fighting another terminal.

```powershell
python touch_git_controller.py --repo C:\path\to\repo
```

Touch controls:

- single tap: `git status`
- double tap: `git pull`
- long press: `git push`

Close MCUXpresso serial terminals, Tera Term, `serial_console.py`, and `git_haptic.py` before starting this listener.
