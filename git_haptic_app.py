import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from git_haptic import run_git_action
from groq_review import review_repo
from haptic_controller import HapticController, load_config


APP_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = Path(r"C:\GitHapticJavaDemo")

COLORS = {
    "window": "#f5f5f7",
    "card": "#ffffff",
    "card_alt": "#fbfbfd",
    "text": "#1d1d1f",
    "muted": "#6e6e73",
    "border": "#d2d2d7",
    "blue": "#007aff",
    "blue_pressed": "#0068d8",
    "red": "#ff3b30",
    "green": "#34c759",
    "log": "#0b1020",
    "log_text": "#e8eefc",
}


class GitHapticApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Git Haptic Controller")
        self.geometry("1080x720")
        self.minsize(920, 620)
        self.configure(bg=COLORS["window"])

        self.output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.controller_process: subprocess.Popen[str] | None = None
        self.controller_reader: threading.Thread | None = None
        self.repo_var = tk.StringVar(value=str(DEFAULT_REPO if DEFAULT_REPO.exists() else Path.cwd()))
        self.ai_review_var = tk.BooleanVar(value=True)
        self.no_haptic_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Stopped")
        self.branch_var = tk.StringVar(value="-")
        self.changes_var = tk.StringVar(value="-")
        self.motors_var = tk.StringVar(value="Unknown")

        self._build_ui()
        self.after(100, self._poll_output_queue)
        self.after(250, self.refresh_repo_status)

    def _build_ui(self) -> None:
        self._configure_style()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        header = ttk.Frame(self, style="App.TFrame", padding=(24, 22, 24, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Git Haptic", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(header, text="A quiet control surface for Git, gestures, motors, and Groq review.", style="Subtle.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 0)
        )
        self.status_pill = tk.Label(
            header,
            textvariable=self.status_var,
            bg="#e9e9ed",
            fg=COLORS["muted"],
            padx=16,
            pady=6,
            font=("Segoe UI", 10, "bold"),
        )
        self.status_pill.grid(row=0, column=3, sticky="e")

        repo_card = ttk.Frame(self, style="Card.TFrame", padding=(18, 14))
        repo_card.grid(row=1, column=0, sticky="ew", padx=24, pady=(8, 12))
        repo_card.columnconfigure(1, weight=1)

        ttk.Label(repo_card, text="Repository", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(repo_card, textvariable=self.repo_var, style="App.TEntry").grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0), ipady=5
        )
        ttk.Button(repo_card, text="Choose", style="Soft.TButton", command=self.browse_repo).grid(
            row=1, column=2, padx=(10, 0), pady=(8, 0)
        )
        ttk.Button(repo_card, text="Refresh", style="Soft.TButton", command=self.refresh_repo_status).grid(
            row=1, column=3, padx=(8, 0), pady=(8, 0)
        )

        controls = ttk.Frame(self, style="App.TFrame", padding=(24, 0, 24, 12))
        controls.grid(row=2, column=0, sticky="ew")
        for index in range(10):
            controls.columnconfigure(index, weight=0)
        controls.columnconfigure(9, weight=1)

        ttk.Button(controls, text="Start", style="Accent.TButton", command=self.start_controller).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(controls, text="Stop", style="Soft.TButton", command=self.stop_controller).grid(row=0, column=1, padx=(0, 14))
        ttk.Button(controls, text="Status", style="Soft.TButton", command=lambda: self.run_git_button("status")).grid(
            row=0, column=2, padx=(0, 8)
        )
        ttk.Button(controls, text="Pull", style="Soft.TButton", command=lambda: self.run_git_button("pull")).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(controls, text="Push", style="Soft.TButton", command=lambda: self.run_git_button("push")).grid(row=0, column=4, padx=(0, 14))
        ttk.Button(controls, text="AI Review", style="Soft.TButton", command=self.run_ai_review).grid(row=0, column=5, padx=(0, 14))
        ttk.Button(controls, text="Success Motor", style="Soft.TButton", command=lambda: self.run_motor("success")).grid(
            row=0, column=6, padx=(0, 8)
        )
        ttk.Button(controls, text="Fail Motor", style="Soft.TButton", command=lambda: self.run_motor("failure")).grid(row=0, column=7)

        options = ttk.Frame(self, style="App.TFrame", padding=(24, 0, 24, 12))
        options.grid(row=4, column=0, sticky="ew")
        ttk.Checkbutton(options, text="AI review before tap-confirmed push", variable=self.ai_review_var, style="App.TCheckbutton").grid(
            row=0, column=0, sticky="w", padx=(0, 16)
        )
        ttk.Checkbutton(options, text="No haptics", variable=self.no_haptic_var, style="App.TCheckbutton").grid(
            row=0, column=1, sticky="w", padx=(0, 16)
        )
        ttk.Checkbutton(options, text="Debug serial output", variable=self.debug_var, style="App.TCheckbutton").grid(row=0, column=2, sticky="w")

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.grid(row=3, column=0, sticky="nsew", padx=24, pady=(0, 14))

        left = ttk.Frame(body, style="Card.TFrame", padding=18)
        left.columnconfigure(1, weight=1)
        body.add(left, weight=1)

        self._add_info_row(left, 0, "Controller", self.status_var)
        self._add_info_row(left, 1, "Branch", self.branch_var)
        self._add_info_row(left, 2, "Changes", self.changes_var)
        self._add_info_row(left, 3, "Motors", self.motors_var)

        ttk.Separator(left).grid(row=4, column=0, columnspan=2, sticky="ew", pady=16)
        ttk.Label(left, text="Gesture Map", style="Section.TLabel").grid(row=5, column=0, columnspan=2, sticky="w")
        gestures = (
            "Idle: no action",
            "1Tap: git status",
            "Tilt/Pull: git pull, then tap approves push",
            "Shake: cancel pending push or toggle motors",
        )
        for offset, text in enumerate(gestures, start=6):
            ttk.Label(left, text=text, style="Body.TLabel", wraplength=310).grid(row=offset, column=0, columnspan=2, sticky="w", pady=4)

        right = ttk.Frame(body, style="Card.TFrame", padding=12)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        body.add(right, weight=3)

        self.log = tk.Text(
            right,
            wrap="word",
            height=24,
            borderwidth=0,
            relief="flat",
            font=("Cascadia Mono", 10),
            bg=COLORS["log"],
            fg=COLORS["log_text"],
            insertbackground=COLORS["log_text"],
            padx=14,
            pady=14,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(right, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("error", foreground="#ff6961")
        self.log.tag_configure("success", foreground="#7ee787")
        self.log.tag_configure("event", foreground="#79c0ff")
        self.log.tag_configure("muted", foreground="#9aa4b2")

        footer = ttk.Frame(self, style="App.TFrame", padding=(24, 0, 24, 18))
        footer.grid(row=5, column=0, sticky="ew")
        ttk.Button(footer, text="Clear Log", style="Soft.TButton", command=self.clear_log).grid(row=0, column=0, sticky="w")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=COLORS["window"], foreground=COLORS["text"])
        style.configure("App.TFrame", background=COLORS["window"])
        style.configure("Card.TFrame", background=COLORS["card"], bordercolor=COLORS["border"], relief="solid", borderwidth=1)
        style.configure("TLabel", background=COLORS["card"], foreground=COLORS["text"])
        style.configure("Title.TLabel", background=COLORS["window"], foreground=COLORS["text"], font=("Segoe UI", 26, "bold"))
        style.configure("Subtle.TLabel", background=COLORS["window"], foreground=COLORS["muted"], font=("Segoe UI", 11))
        style.configure("Caption.TLabel", background=COLORS["card"], foreground=COLORS["muted"], font=("Segoe UI", 9, "bold"))
        style.configure("Section.TLabel", background=COLORS["card"], foreground=COLORS["text"], font=("Segoe UI", 12, "bold"))
        style.configure("Body.TLabel", background=COLORS["card"], foreground=COLORS["muted"], font=("Segoe UI", 10))
        style.configure("Value.TLabel", background=COLORS["card"], foreground=COLORS["text"], font=("Segoe UI", 10, "bold"))
        style.configure("App.TCheckbutton", background=COLORS["window"], foreground=COLORS["muted"])
        style.map("App.TCheckbutton", background=[("active", COLORS["window"])])
        style.configure(
            "App.TEntry",
            fieldbackground=COLORS["card_alt"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=(10, 7),
        )
        style.configure(
            "Soft.TButton",
            background="#ececf0",
            foreground=COLORS["text"],
            bordercolor="#ececf0",
            lightcolor="#ececf0",
            darkcolor="#ececf0",
            padding=(14, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.map("Soft.TButton", background=[("active", "#e2e2e7"), ("pressed", "#d8d8de")])
        style.configure(
            "Accent.TButton",
            background=COLORS["blue"],
            foreground="#ffffff",
            bordercolor=COLORS["blue"],
            lightcolor=COLORS["blue"],
            darkcolor=COLORS["blue"],
            padding=(18, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", COLORS["blue_pressed"]), ("pressed", COLORS["blue_pressed"])],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure("TPanedwindow", background=COLORS["window"])
        style.configure("Vertical.TScrollbar", background="#e2e2e7", troughcolor=COLORS["card"], bordercolor=COLORS["card"])

    def _add_info_row(self, parent: ttk.Frame, row: int, label: str, value: tk.StringVar) -> None:
        ttk.Label(parent, text=label, style="Caption.TLabel").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Label(parent, textvariable=value, style="Value.TLabel", wraplength=320).grid(
            row=row, column=1, sticky="w", padx=(12, 0), pady=6
        )

    def browse_repo(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.repo_var.get() or str(Path.cwd()))
        if selected:
            self.repo_var.set(selected)
            self.refresh_repo_status()

    def repo_path(self) -> Path:
        return Path(self.repo_var.get()).expanduser().resolve()

    def append_log(self, text: str, tag: str | None = None) -> None:
        self.log.insert("end", text, tag)
        self.log.see("end")

    def clear_log(self) -> None:
        self.log.delete("1.0", "end")

    def enqueue_log(self, text: str, tag: str | None = None) -> None:
        self.output_queue.put((text, tag or ""))

    def _poll_output_queue(self) -> None:
        while True:
            try:
                text, tag = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(text, tag or None)
        self.after(100, self._poll_output_queue)

    def run_background(self, label: str, target) -> None:
        def wrapped() -> None:
            try:
                target()
            except Exception as error:
                self.enqueue_log(f"\n{label} failed: {error}\n", "error")

        threading.Thread(target=wrapped, daemon=True).start()

    def start_controller(self) -> None:
        if self.controller_process and self.controller_process.poll() is None:
            messagebox.showinfo("Controller already running", "The controller is already listening for gestures.")
            return

        repo = self.repo_path()
        if not repo.is_dir():
            messagebox.showerror("Invalid repo", f"Repository path does not exist:\n{repo}")
            return

        command = [
            sys.executable,
            str(APP_DIR / "ml_git_controller.py"),
            "--repo",
            str(repo),
        ]
        if self.ai_review_var.get():
            command.append("--ai-review")
        if self.no_haptic_var.get():
            command.append("--no-haptic")
        if self.debug_var.get():
            command.append("--debug")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        self.append_log("\nStarting controller...\n", "event")
        self.append_log(f"{' '.join(command)}\n", "muted")
        self.controller_process = subprocess.Popen(
            command,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self.status_var.set("Running")
        self.controller_reader = threading.Thread(target=self._read_controller_output, daemon=True)
        self.controller_reader.start()

    def _read_controller_output(self) -> None:
        process = self.controller_process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            tag = "event" if "Gesture" in line or "Listening" in line else ""
            if "failed" in line.lower() or "error" in line.lower() or "blocked" in line.lower():
                tag = "error"
            self.enqueue_log(line, tag)
            if "Motors enabled" in line:
                self.motors_var.set("Enabled")
            elif "Motors disabled" in line:
                self.motors_var.set("Disabled")
            if "Gesture action:" in line or "Exit code:" in line:
                self.after(0, self.refresh_repo_status)
        returncode = process.wait()
        self.status_var.set("Stopped")
        self.enqueue_log(f"\nController stopped with exit code {returncode}.\n", "muted")

    def stop_controller(self) -> None:
        process = self.controller_process
        if process is None or process.poll() is not None:
            self.status_var.set("Stopped")
            return
        self.append_log("\nStopping controller...\n", "event")
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        self.status_var.set("Stopped")

    def run_git_button(self, action: str) -> None:
        def task() -> None:
            repo = self.repo_path()
            self.enqueue_log(f"\nRunning git {action}...\n", "event")
            result = run_git_action(action, repo)
            self.enqueue_log(f"Repo: {result.repo}\nCommand: {' '.join(result.command)}\nExit code: {result.returncode}\n")
            if result.stdout.strip():
                self.enqueue_log(f"\n--- stdout ---\n{result.stdout.rstrip()}\n")
            if result.stderr.strip():
                self.enqueue_log(f"\n--- stderr ---\n{result.stderr.rstrip()}\n", "error" if not result.succeeded else "")
            self.enqueue_log("\n")
            self.after(0, self.refresh_repo_status)

        self.run_background(f"git {action}", task)

    def run_ai_review(self) -> None:
        def task() -> None:
            repo = self.repo_path()
            self.enqueue_log("\nRunning Groq AI review...\n", "event")
            review = review_repo(repo)
            self.enqueue_log(f"Summary: {review.summary}\n")
            self.enqueue_log(f"Risk: {review.risk_level}\n")
            if review.has_no_diff:
                self.enqueue_log("No staged or unstaged changes found.\n", "muted")
            if review.possible_issues:
                self.enqueue_log("\nPossible issues:\n")
                for issue in review.possible_issues:
                    self.enqueue_log(f"- {issue}\n")
            if review.next_steps:
                self.enqueue_log("\nNext steps:\n")
                for step in review.next_steps:
                    self.enqueue_log(f"- {step}\n")
            self.enqueue_log("\n")

        self.run_background("AI review", task)

    def run_motor(self, pattern: str) -> None:
        def task() -> None:
            config = load_config(APP_DIR / "config.json")
            self.enqueue_log(f"\nPlaying {pattern} motor pattern...\n", "event")
            with HapticController(config) as controller:
                if pattern == "success":
                    controller.success()
                    self.motors_var.set("Success pulse sent")
                elif pattern == "failure":
                    controller.failure()
                    self.motors_var.set("Failure pulse sent")
            self.enqueue_log("Motor command complete.\n")

        self.run_background("motor command", task)

    def refresh_repo_status(self) -> None:
        repo = self.repo_path()
        if not repo.is_dir():
            self.branch_var.set("Invalid repo")
            self.changes_var.set("-")
            return

        def run(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(command, cwd=repo, capture_output=True, text=True)

        branch = run(["git", "branch", "--show-current"])
        if branch.returncode == 0 and branch.stdout.strip():
            self.branch_var.set(branch.stdout.strip())
        else:
            self.branch_var.set("(not a Git repo)")

        status = run(["git", "status", "--short"])
        if status.returncode != 0:
            self.changes_var.set("Git status failed")
            return

        lines = [line for line in status.stdout.splitlines() if line.strip()]
        if not lines:
            self.changes_var.set("Clean")
        elif len(lines) == 1:
            self.changes_var.set(lines[0])
        else:
            self.changes_var.set(f"{len(lines)} changed files")

    def destroy(self) -> None:
        self.stop_controller()
        super().destroy()


def main() -> int:
    app = GitHapticApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
