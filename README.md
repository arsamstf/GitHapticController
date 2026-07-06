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

Explain failed pull/push operations with Groq:

```powershell
python git_haptic.py pull --repo C:\path\to\repo --ai-help
python git_haptic.py push --repo C:\path\to\repo --ai-help
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
- long press or triple tap: stage, commit, and push

Close MCUXpresso serial terminals, Tera Term, `serial_console.py`, and `git_haptic.py` before starting this listener.

## ML Gesture Controller

Use this after flashing the deployed Time Series Studio 4-class model. The script reads prediction lines like `PREDICT ... CLASS: 1`, waits for stable predictions, and maps them to Git commands.

```powershell
python ml_git_controller.py --repo C:\GitHapticJavaDemo
```

Class mapping:

- `CLASS: 0`: Idle, no action
- `CLASS: 1`: 1Tap, runs `git status` or confirms pending push
- `CLASS: 2`: ShakeCancel, cancels pending push or toggles motors
- `CLASS: 3`: TiltForward, runs `git pull`, then opens push approval

Useful test modes:

```powershell
python ml_git_controller.py --repo C:\GitHapticJavaDemo --debug
python ml_git_controller.py --repo C:\GitHapticJavaDemo --no-haptic
python ml_git_controller.py --repo C:\GitHapticJavaDemo --no-setup
```

If `git status` feels too sensitive, make status stricter:

```powershell
python ml_git_controller.py --repo C:\GitHapticJavaDemo --ai-review --status-confidence 0.95 --status-stable-count 4
```

Push confirmation:

- TiltForward runs `git pull`.
- If pull succeeds, the script waits for push approval.
- 1Tap confirms and runs `git push`.
- ShakeCancel cancels the pending push.
- If no push is pending, ShakeCancel toggles motor feedback on/off.
- If you do nothing, the pending push expires.

Push confirmation and shake are handled before the normal status filters, so a 1Tap after pull should confirm push instead of becoming `git status`.

Tune or disable the approval window:

```powershell
python ml_git_controller.py --repo C:\GitHapticJavaDemo --push-confirm-window 15
python ml_git_controller.py --repo C:\GitHapticJavaDemo --push-confirm-window 0
```

### Groq AI Review

Store your Groq key in `.env`:

```text
GROQ_API_KEY=your_key_here
```

Check the API connection:

```powershell
python groq_review.py check
```

Review the current Git diff:

```powershell
python groq_review.py review --repo C:\GitHapticJavaDemo
```

Use AI review before a tap-confirmed ML push:

```powershell
python ml_git_controller.py --repo C:\GitHapticJavaDemo --ai-review
```

AI review can warn or block high-risk pushes, but it does not edit files, resolve conflicts, or push by itself.

The ML controller also asks Groq to explain failed pull/push operations by default. Disable that when testing without internet:

```powershell
python ml_git_controller.py --repo C:\GitHapticJavaDemo --no-ai-help
```

## Conflict Pilot

Conflict Pilot runs `git pull` or `git push`, classifies common failures, and waits for touch approval before running one safe recovery command.
It also asks Groq to explain the failure and safe next steps.

```powershell
python conflict_pilot.py push --repo C:\GitHapticController
python conflict_pilot.py pull --repo C:\GitHapticController
```

Run without Groq explanations:

```powershell
python conflict_pilot.py push --repo C:\GitHapticController --no-ai-help
```

Physical controls after a Git failure:

- single tap: approve the suggested safe recovery command
- double tap: print the explanation again
- long press: retry the original command

Safety rules:

- Groq explains failures, but does not run commands
- no command runs without touch approval
- no file conflicts are resolved automatically
- after `git pull --rebase`, the script does not auto-push

## Commit Pilot

Commit Pilot stages, commits, and pushes basic changes without Groq. It uses simple file-based commit messages such as `Update Main.java`, `Update README`, or `Update project files`.

```powershell
python commit_pilot.py --repo C:\GitHapticJavaDemo
```

Physical controls:

- single tap: `git status`
- double tap: `git add -A`
- long press: `git add -A`, basic `git commit -m ...`, then `git push`

Use a fixed message when you want predictable demo commits:

```powershell
python commit_pilot.py --repo C:\GitHapticJavaDemo --message "Test controller commit"
```
