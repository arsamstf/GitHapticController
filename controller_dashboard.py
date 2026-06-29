import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from haptic_controller import find_serial_port, load_config
from touch_git_controller import SharedSerialHaptics, auto_reset_board


ACTIONS = ["status", "pull", "push", "commit flow", "recovery"]
TOUCH_EVENT_PATTERN = re.compile(r"\bevent touch (single|double|long)\b")
ACCEL_EVENT_PATTERN = re.compile(r"\bevent accel (tilt_left|tilt_right|shake)\b")
READY_PATTERN = re.compile(r"\bevent touch ready\b")

NO_UPSTREAM_BRANCH = "NO_UPSTREAM_BRANCH"
PUSH_REJECTED_REMOTE_CHANGES = "PUSH_REJECTED_REMOTE_CHANGES"
MERGE_CONFLICT = "MERGE_CONFLICT"
REBASE_IN_PROGRESS = "REBASE_IN_PROGRESS"
UNCOMMITTED_CHANGES_BLOCKING_PULL = "UNCOMMITTED_CHANGES_BLOCKING_PULL"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


@dataclass
class GitResult:
    label: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


@dataclass
class RecoverySuggestion:
    category: str = "None"
    explanation: str = "No recovery pending."
    command: list[str] | None = None


@dataclass
class DashboardState:
    repo: Path
    selected_index: int = 0
    branch: str = "unknown"
    staged_files: list[str] = field(default_factory=list)
    unstaged_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    repo_status: str = "Checking repository."
    ahead_behind: str = "unknown"
    last_git: str = "No Git command yet."
    last_git_detail: str = "Tap once to run the selected action."
    last_event: str = "Waiting for board event."
    last_haptic: str = "None"
    recovery: RecoverySuggestion = field(default_factory=RecoverySuggestion)
    pending_approval: bool = False
    last_failed_command: list[str] | None = None
    debug_lines: list[str] = field(default_factory=list)

    @property
    def selected_action(self) -> str:
        return ACTIONS[self.selected_index]


def run_git(repo: Path, command: list[str], label: str) -> GitResult:
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    return GitResult(
        label=label,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def current_branch(repo: Path) -> str:
    result = run_git(repo, ["git", "branch", "--show-current"], "branch")
    branch = result.stdout.strip()
    return branch or "HEAD"


def file_state(repo: Path) -> tuple[list[str], list[str], list[str]]:
    result = run_git(repo, ["git", "status", "--porcelain"], "status")
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        filename = line[3:].strip()
        if index_status == "?" and worktree_status == "?":
            untracked.append(filename)
            continue
        if index_status != " ":
            staged.append(filename)
        if worktree_status != " ":
            unstaged.append(filename)

    return staged, unstaged, untracked


def ahead_behind(repo: Path) -> str:
    result = run_git(
        repo,
        ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        "ahead behind",
    )
    if result.returncode != 0:
        return "no upstream"

    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return "unknown"

    ahead, behind = parts
    return f"ahead {ahead} / behind {behind}"


def staged_files(repo: Path) -> list[str]:
    result = run_git(repo, ["git", "diff", "--cached", "--name-only"], "staged files")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


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


def git_dir(repo: Path) -> Path | None:
    result = run_git(repo, ["git", "rev-parse", "--git-dir"], "git dir")
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else (repo / path).resolve()


def has_merge_conflict(output: str) -> bool:
    markers = (
        "merge conflict",
        "fix conflicts",
        "unmerged paths",
        "you have unmerged paths",
        "automatic merge failed",
        "both modified:",
    )
    lowered = output.lower()
    return any(marker in lowered for marker in markers)


def classify_failure(repo: Path, result: GitResult) -> RecoverySuggestion:
    output = result.combined_output.lower()
    repo_git_dir = git_dir(repo)

    if has_merge_conflict(output):
        return RecoverySuggestion(
            MERGE_CONFLICT,
            "Real file conflicts were found. Resolve files manually; no automatic file choice will be made.",
        )

    if repo_git_dir and ((repo_git_dir / "rebase-merge").exists() or (repo_git_dir / "rebase-apply").exists()):
        return RecoverySuggestion(
            REBASE_IN_PROGRESS,
            "A rebase is already in progress. Resolve files manually, then continue or abort the rebase.",
        )

    if "has no upstream branch" in output:
        branch = current_branch(repo)
        return RecoverySuggestion(
            NO_UPSTREAM_BRANCH,
            "This branch has no upstream remote branch yet.",
            ["git", "push", "--set-upstream", "origin", branch],
        )

    rejected_markers = (
        "failed to push some refs",
        "fetch first",
        "non-fast-forward",
        "updates were rejected",
        "tip of your current branch is behind",
    )
    if result.label == "push" and any(marker in output for marker in rejected_markers):
        return RecoverySuggestion(
            PUSH_REJECTED_REMOTE_CHANGES,
            "The remote branch has commits you do not have locally.",
            ["git", "pull", "--rebase"],
        )

    uncommitted_markers = (
        "your local changes to the following files would be overwritten",
        "please commit your changes or stash them before you merge",
        "cannot pull with rebase: you have unstaged changes",
        "cannot rebase: you have unstaged changes",
    )
    if any(marker in output for marker in uncommitted_markers):
        return RecoverySuggestion(
            UNCOMMITTED_CHANGES_BLOCKING_PULL,
            "Local uncommitted changes are blocking this Git operation.",
        )

    return RecoverySuggestion(UNKNOWN_FAILURE, "This Git failure is not recognized yet.")


def refresh_repo_state(state: DashboardState) -> None:
    state.branch = current_branch(state.repo)
    state.staged_files, state.unstaged_files, state.untracked_files = file_state(state.repo)
    state.ahead_behind = ahead_behind(state.repo)

    if not state.staged_files and not state.unstaged_files and not state.untracked_files:
        state.repo_status = "clean"
    else:
        state.repo_status = (
            f"{len(state.staged_files)} staged, "
            f"{len(state.unstaged_files)} unstaged, "
            f"{len(state.untracked_files)} untracked"
        )


def add_debug(state: DashboardState, text: str, enabled: bool) -> None:
    if enabled:
        state.debug_lines.append(text)
        state.debug_lines = state.debug_lines[-8:]


def clean_detail(text: str, max_lines: int = 6) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines] + ["..."])
    return "\n".join(lines)


def summarize_result(state: DashboardState, result: GitResult, debug: bool) -> None:
    command_text = " ".join(result.command)
    status = "OK" if result.succeeded else "FAILED"
    state.last_git = f"{status}: {command_text}"

    if result.label == "status" and result.stdout.strip():
        state.last_git_detail = clean_detail(result.stdout)
    elif result.succeeded and result.stdout.strip():
        state.last_git_detail = clean_detail(result.stdout)
    elif result.succeeded:
        state.last_git_detail = "Command completed successfully."
    elif result.stderr.strip():
        state.last_git_detail = clean_detail(result.stderr)
    elif result.stdout.strip():
        state.last_git_detail = clean_detail(result.stdout)
    else:
        state.last_git_detail = "Command failed with no output."

    add_debug(state, f"$ {command_text}", debug)
    if result.stdout.strip():
        add_debug(state, result.stdout.strip(), debug)
    if result.stderr.strip():
        add_debug(state, result.stderr.strip(), debug)


def play_feedback(state: DashboardState, haptics: SharedSerialHaptics, result: GitResult) -> None:
    if result.succeeded and result.label == "push":
        haptics.push_success()
        state.last_haptic = "Balanced long pulse"
    elif result.succeeded:
        haptics.success()
        state.last_haptic = "Balanced short pulse"
    elif has_merge_conflict(result.combined_output):
        haptics.merge_conflict()
        state.last_haptic = "Unbalanced conflict pattern"
    else:
        haptics.failure()
        state.last_haptic = "Unbalanced failure pulse"


def run_and_handle(
    state: DashboardState,
    haptics: SharedSerialHaptics,
    command: list[str],
    label: str,
    debug: bool,
) -> GitResult:
    result = run_git(state.repo, command, label)
    summarize_result(state, result, debug)
    play_feedback(state, haptics, result)
    refresh_repo_state(state)

    if result.succeeded:
        state.recovery = RecoverySuggestion()
        state.pending_approval = False
        state.last_failed_command = None
    else:
        state.recovery = classify_failure(state.repo, result)
        state.pending_approval = state.recovery.command is not None
        state.last_failed_command = command

    return result


def run_selected_action(state: DashboardState, haptics: SharedSerialHaptics, debug: bool) -> None:
    action = state.selected_action
    if action == "status":
        run_and_handle(state, haptics, ["git", "status"], "status", debug)
    elif action == "pull":
        run_and_handle(state, haptics, ["git", "pull"], "pull", debug)
    elif action == "push":
        run_and_handle(state, haptics, ["git", "push"], "push", debug)
    elif action == "commit flow":
        if state.staged_files:
            commit_and_push(state, haptics, debug)
        else:
            stage_all(state, haptics, debug)
    elif action == "recovery":
        approve_recovery(state, haptics, debug)


def stage_all(state: DashboardState, haptics: SharedSerialHaptics, debug: bool) -> None:
    run_and_handle(state, haptics, ["git", "add", "-A"], "stage", debug)


def commit_and_push(state: DashboardState, haptics: SharedSerialHaptics, debug: bool) -> None:
    files = staged_files(state.repo)
    if not files:
        state.last_git = "No staged changes to commit."
        state.last_git_detail = "Stage files first with a double tap."
        haptics.success()
        state.last_haptic = "Balanced short pulse"
        refresh_repo_state(state)
        return

    message = basic_commit_message(files)
    commit = run_and_handle(state, haptics, ["git", "commit", "-m", message], "commit", debug)
    if commit.succeeded:
        run_and_handle(state, haptics, ["git", "push"], "push", debug)


def approve_recovery(state: DashboardState, haptics: SharedSerialHaptics, debug: bool) -> None:
    if not state.pending_approval or not state.recovery.command:
        state.last_git = "No safe recovery command is pending."
        state.last_git_detail = state.recovery.explanation
        return
    run_and_handle(state, haptics, state.recovery.command, "recovery", debug)
    if state.last_git.startswith("OK:"):
        state.last_git += " | Retry original action manually."


def retry_last_failure(state: DashboardState, haptics: SharedSerialHaptics, debug: bool) -> None:
    if not state.last_failed_command:
        state.last_git = "No failed command to retry."
        state.last_git_detail = "Shake retries only after a failed Git command."
        return
    label = state.last_failed_command[1] if len(state.last_failed_command) > 1 else "retry"
    run_and_handle(state, haptics, state.last_failed_command, label, debug)


def handle_event(state: DashboardState, haptics: SharedSerialHaptics, line: str, debug: bool) -> None:
    touch = TOUCH_EVENT_PATTERN.search(line)
    accel = ACCEL_EVENT_PATTERN.search(line)

    if READY_PATTERN.search(line):
        state.last_event = "Touch firmware ready"
        return

    if touch:
        event = touch.group(1)
        state.last_event = f"Touch: {event}"
        if event == "single":
            if state.pending_approval:
                approve_recovery(state, haptics, debug)
            elif state.staged_files:
                commit_and_push(state, haptics, debug)
            else:
                run_selected_action(state, haptics, debug)
        elif event == "double":
            stage_all(state, haptics, debug)
        elif event == "long":
            state.pending_approval = False
            state.last_git = "Recovery approval canceled."
            state.last_git_detail = "No recovery command will run."
        return

    if accel:
        event = accel.group(1)
        state.last_event = f"Accelerometer: {event}"
        if event == "tilt_left":
            state.selected_index = (state.selected_index - 1) % len(ACTIONS)
        elif event == "tilt_right":
            state.selected_index = (state.selected_index + 1) % len(ACTIONS)
        elif event == "shake":
            if state.pending_approval:
                state.pending_approval = False
                state.recovery = RecoverySuggestion()
                state.last_git = "Recovery approval canceled by shake."
                state.last_git_detail = "No recovery command will run."
            else:
                retry_last_failure(state, haptics, debug)


def render_dashboard(state: DashboardState, debug: bool) -> Panel:
    layout = Table.grid(expand=True)
    layout.add_column(ratio=1)

    status = Table.grid(expand=True)
    status.add_column(justify="left")
    status.add_column(justify="left")
    status.add_row("Repo", str(state.repo))
    status.add_row("Branch", state.branch)
    status.add_row("Repo status", state.repo_status)
    status.add_row("Remote", state.ahead_behind)
    status.add_row("Selected", state.selected_action)
    status.add_row("Last event", state.last_event)
    status.add_row("Last haptic", state.last_haptic)

    files = Table(title="Working Tree", box=box.SIMPLE_HEAVY)
    files.add_column("Staged")
    files.add_column("Unstaged")
    files.add_column("Untracked")
    max_rows = max(len(state.staged_files), len(state.unstaged_files), len(state.untracked_files), 1)
    for index in range(min(max_rows, 8)):
        files.add_row(
            state.staged_files[index] if index < len(state.staged_files) else "",
            state.unstaged_files[index] if index < len(state.unstaged_files) else "",
            state.untracked_files[index] if index < len(state.untracked_files) else "",
        )
    if max_rows > 8:
        files.add_row("...", "...", "...")

    recovery = Table.grid(expand=True)
    recovery.add_column()
    recovery.add_row(f"Last Git: {state.last_git}")
    recovery.add_row(state.last_git_detail)
    recovery.add_row(f"Recovery: {state.recovery.category}")
    recovery.add_row(state.recovery.explanation)
    if state.recovery.command:
        recovery.add_row("Suggested: " + " ".join(state.recovery.command))
        recovery.add_row("Single tap approves. Shake cancels.")

    controls = "Single tap: selected action/approve | Double tap: stage | Tilt: select | Shake: cancel/retry | Ctrl+C: stop"

    layout.add_row(Panel(status, title="Git Haptic Controller", box=box.ROUNDED))
    layout.add_row(files)
    layout.add_row(Panel(recovery, title="Result / Recovery", box=box.ROUNDED))
    layout.add_row(Panel(controls, title="Controls", box=box.ROUNDED))

    if debug:
        debug_text = "\n".join(state.debug_lines[-8:]) or "No debug lines yet."
        layout.add_row(Panel(debug_text, title="Debug", box=box.ROUNDED))

    return Panel(layout, box=box.DOUBLE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean terminal dashboard for Git Haptic Controller.")
    parser.add_argument("--repo", default=".", type=Path, help="Repository path.")
    parser.add_argument("--config", default="config.json", type=Path, help="Path to config.json.")
    parser.add_argument("--debug", action="store_true", help="Show raw serial and Git details.")
    parser.add_argument("--no-auto-reset", action="store_true", help="Do not auto-reset the board on startup.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = args.repo.resolve()
    if not repo.exists() or not repo.is_dir():
        print(f"Repository path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2

    config = load_config(args.config)
    port = find_serial_port() if config.serial_port == "auto" else config.serial_port
    state = DashboardState(repo=repo)
    refresh_repo_state(state)
    console = Console()

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
            haptics = SharedSerialHaptics(
                board=board,
                command_delay_seconds=config.command_delay_seconds,
                line_ending=config.line_ending,
                show_echoes=False,
            )

            if not args.no_auto_reset:
                auto_reset_board(board)

            with Live(render_dashboard(state, args.debug), console=console, refresh_per_second=8) as live:
                while True:
                    line = board.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        if args.debug:
                            add_debug(state, f"serial: {line}", True)
                        handle_event(state, haptics, line, args.debug)
                    refresh_repo_state(state)
                    live.update(render_dashboard(state, args.debug))
                    time.sleep(0.05)

    except KeyboardInterrupt:
        console.print("\nStopped.")
        return 0
    except serial.SerialException as error:
        console.print(f"Serial failed: {error}", style="red")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
