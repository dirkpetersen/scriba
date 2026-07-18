"""Standalone Scriba installer (DESIGN.md §12).

Stdlib only, deliberately -- this must run with a bare `python installer/install.py`
on a machine that has none of Scriba's dependencies yet, because its job is to
*build* the uv-managed venv that provides them. Do not add third-party imports
here (tkinter ships with standard CPython on Windows, so the GUI is "free").

Flow (same order in both GUI and --unattended modes):
  1. hardware check (GPU + VRAM via nvidia-smi, disk space, OS) -- warns only
  2. ensure `uv` is on PATH, installing it via the official script if not
  3. `uv sync` in the repo root, streamed into the log
  4. Start Menu shortcut (a hidden-window .vbs launcher + a .lnk pointing at it)
  5. optional autostart registration (`uv run ... scriba --autostart`)
  6. GUI only: a "Launch Scriba now" button. Unattended installs must not pop
     up a tray icon/window unexpectedly, so they never auto-launch the app.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller --onefile: __file__ resolves inside the extracted temp
        # dir (sys._MEIPASS), not where ScribaSetup.exe actually sits -- use
        # sys.executable instead. ScribaSetup.exe is built to installer/dist/,
        # one level deeper than this source file (installer/install.py), so
        # this needs an extra .parent to land on the repo root.
        return Path(sys.executable).resolve().parent.parent.parent
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _repo_root()
ICON_PATH = REPO_ROOT / "scriba.ico"
LAUNCHER_VBS = REPO_ROOT / "scriba_launch.vbs"

MIN_DISK_FREE_GB = 10.0
# The RTX 3050 Laptop in CLAUDE.md is a 4096 MiB card; warn a bit below that so
# the target machine itself doesn't trip the warning, while smaller/older GPUs do.
MIN_VRAM_MB = 3500

logger = logging.getLogger("scriba.installer")


# ---------------------------------------------------------------------------
# Pure helpers -- parsing / path construction, covered by --selftest below.
# ---------------------------------------------------------------------------


def parse_nvidia_smi_csv(output: str) -> list[tuple[str, int]]:
    """Parse `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader` stdout."""
    gpus = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        name, mem = parts
        mem_number = mem.split()[0] if mem else ""  # "4096 MiB" -> "4096"
        try:
            vram_mb = int(mem_number)
        except ValueError:
            continue
        gpus.append((name, vram_mb))
    return gpus


def gpu_warnings(gpus: list[tuple[str, int]], min_vram_mb: int = MIN_VRAM_MB) -> list[str]:
    if not gpus:
        return [
            "No NVIDIA GPU detected (nvidia-smi missing or returned nothing). "
            "Scriba will fall back to CPU, which will be noticeably slower (DESIGN.md §9)."
        ]
    warnings = []
    for name, vram_mb in gpus:
        if vram_mb < min_vram_mb:
            warnings.append(
                f"GPU '{name}' has only {vram_mb} MiB VRAM (recommended >= {min_vram_mb} MiB). "
                "Scriba may drop to a smaller model or CPU, which will be slower."
            )
    return warnings


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024**3)


def is_windows() -> bool:
    return platform.system() == "Windows"


def local_appdata_dir() -> Path:
    """Mirrors scriba.config.data_dir() without importing it.

    scriba.config imports tomlkit (third-party, not installed at this point in
    a fresh install) so it can't be used here -- see task brief / CLAUDE.md.
    """
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Scriba"


def install_log_path() -> Path:
    return local_appdata_dir() / "logs" / "install.log"


def start_menu_shortcut_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Scriba.lnk"


def normalize_argv(argv: list[str]) -> list[str]:
    """Accept Windows-installer-convention aliases (/S, /s, /silent) for --unattended."""
    aliases = {"/s": "--unattended", "/silent": "--unattended"}
    return [aliases.get(a.lower(), a) for a in argv]


# ---------------------------------------------------------------------------
# uv / subprocess plumbing
# ---------------------------------------------------------------------------


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    # The official installer writes here; PATH may not be refreshed in this
    # process even right after installing it, so also check directly.
    candidate = Path.home() / ".local" / "bin" / "uv.exe"
    if candidate.exists():
        return str(candidate)
    return None


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_windows() else 0


def run_streamed(cmd: list[str], cwd: Path | None, log) -> int:
    """Run `cmd`, streaming stdout+stderr line-by-line into `log`. Returns exit code."""
    log(f"$ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=_no_window_flags(),
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    return proc.wait()


def install_uv(log) -> str | None:
    """Runs the official installer script (DESIGN.md-adjacent: never hand-roll wheel fetching)."""
    log("uv not found on PATH; installing via the official installer script...")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "irm https://astral.sh/uv/install.ps1 | iex",
    ]
    rc = run_streamed(cmd, None, log)
    if rc != 0:
        log(f"uv installer script exited with code {rc}")
        return None
    return find_uv()


def write_launcher_script(uv_path: str, repo_root: Path = REPO_ROOT) -> Path:
    """A tiny hidden-window VBScript launcher -- avoids a console flash on double-click."""
    vbs = (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.CurrentDirectory = "{repo_root}"\n'
        f'shell.Run """{uv_path}"" run --project ""{repo_root}"" scriba", 0, False\n'
    )
    LAUNCHER_VBS.write_text(vbs, encoding="utf-8")
    return LAUNCHER_VBS


def create_shortcut(uv_path: str, log, repo_root: Path = REPO_ROOT) -> Path:
    """Creates the Start Menu .lnk via WScript.Shell (no extra Python packages needed)."""
    vbs_path = write_launcher_script(uv_path, repo_root)
    lnk_path = start_menu_shortcut_path()
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    ps1_source = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{lnk_path}")
$Shortcut.TargetPath = "{vbs_path}"
$Shortcut.WorkingDirectory = "{repo_root}"
$Shortcut.IconLocation = "{ICON_PATH},0"
$Shortcut.Description = "Scriba voice dictation"
$Shortcut.Save()
"""
    fd, ps1_name = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    ps1_path = Path(ps1_name)
    ps1_path.write_text(ps1_source, encoding="utf-8")
    try:
        rc = run_streamed(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1_path)],
            None,
            log,
        )
        if rc != 0:
            raise RuntimeError(f"shortcut PowerShell script exited with code {rc}")
    finally:
        ps1_path.unlink(missing_ok=True)
    log(f"Created Start Menu shortcut at {lnk_path}")
    return lnk_path


def check_hardware(repo_root: Path, log) -> list[str]:
    warnings: list[str] = []
    log(f"Python {platform.python_version()} on {platform.system()} {platform.release()}")
    if not is_windows():
        warnings.append(f"This installer targets Windows; detected {platform.system()}.")

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        gpus = parse_nvidia_smi_csv(result.stdout) if result.returncode == 0 else []
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        gpus = []
    for name, vram_mb in gpus:
        log(f"GPU detected: {name} ({vram_mb} MiB)")
    warnings.extend(gpu_warnings(gpus))

    free_gb = disk_free_gb(repo_root)
    log(f"Free disk space: {free_gb:.1f} GB")
    if free_gb < MIN_DISK_FREE_GB:
        warnings.append(
            f"Only {free_gb:.1f} GB free disk space (recommend >= {MIN_DISK_FREE_GB:.0f} GB "
            "for the model weights + CUDA dependencies)."
        )

    for w in warnings:
        log(f"WARNING: {w}")
    return warnings


# ---------------------------------------------------------------------------
# logging setup
# ---------------------------------------------------------------------------


def setup_logging(gui_queue: queue.Queue[str] | None = None) -> logging.Logger:
    log_path = install_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger("scriba.installer")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    log.addHandler(stream_handler)

    if gui_queue is not None:

        class QueueHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                gui_queue.put(self.format(record))

        queue_handler = QueueHandler()
        queue_handler.setFormatter(fmt)
        log.addHandler(queue_handler)

    return log


# ---------------------------------------------------------------------------
# unattended mode
# ---------------------------------------------------------------------------


def run_unattended(args: argparse.Namespace) -> int:
    log = setup_logging().info
    log("Scriba unattended install starting.")
    log(f"Log file: {install_log_path()}")
    log(f"Repo root: {REPO_ROOT}")

    warnings = check_hardware(REPO_ROOT, log)
    if warnings:
        log(f"Continuing despite {len(warnings)} hardware warning(s) (unattended mode).")

    uv_path = find_uv() or install_uv(log)
    if uv_path is None:
        log("ERROR: could not install or locate uv.")
        return 2
    log(f"Using uv at {uv_path}")

    rc = run_streamed([uv_path, "sync"], REPO_ROOT, log)
    if rc != 0:
        log(f"ERROR: uv sync failed with exit code {rc}")
        return 3
    log("uv sync completed successfully.")

    try:
        create_shortcut(uv_path, log)
    except Exception as exc:
        # Best-effort: a missing shortcut doesn't stop Scriba from working via
        # `uv run scriba`, so this doesn't fail the whole install.
        log(f"WARNING: shortcut creation failed: {exc}")

    if args.autostart:
        rc = run_streamed(
            [uv_path, "run", "--project", str(REPO_ROOT), "scriba", "--autostart"],
            REPO_ROOT,
            log,
        )
        if rc != 0:
            log(f"WARNING: enabling autostart failed with exit code {rc}")
        else:
            log("Autostart enabled.")

    log("Unattended install complete. Scriba was not launched (silent installs stay silent).")
    return 0


# ---------------------------------------------------------------------------
# GUI mode
# ---------------------------------------------------------------------------


def run_gui(args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    log_queue: queue.Queue[str] = queue.Queue()
    log = setup_logging(gui_queue=log_queue).info
    log(f"Log file: {install_log_path()}")
    log(f"Repo root: {REPO_ROOT}")

    state = {"success": None, "uv_path": None}

    root = tk.Tk()
    root.title("Scriba Setup")
    root.geometry("640x520")
    root.minsize(520, 420)
    try:
        root.iconbitmap(str(ICON_PATH))
    except Exception:
        pass

    ttk.Label(root, text="Scriba Setup", font=("Segoe UI", 14, "bold")).pack(
        anchor="w", padx=12, pady=(12, 4)
    )

    log("Checking hardware...")
    warnings = check_hardware(REPO_ROOT, log)

    if warnings:
        warn_text = "\n".join(f"- {w}" for w in warnings)
        tk.Label(
            root,
            text="Warnings:\n" + warn_text,
            fg="#a05a00",
            justify="left",
            wraplength=600,
        ).pack(anchor="w", padx=12, pady=(0, 4))

        def on_continue_toggle() -> None:
            install_btn.config(state="normal" if continue_var.get() else "disabled")

        continue_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            root,
            text="I understand, continue anyway",
            variable=continue_var,
            command=on_continue_toggle,
        ).pack(anchor="w", padx=12)
    else:
        ttk.Label(root, text="Hardware check passed.").pack(anchor="w", padx=12)

    autostart_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        root, text="Start Scriba automatically when I log in", variable=autostart_var
    ).pack(anchor="w", padx=12, pady=(8, 4))

    status_label = ttk.Label(root, text="Ready to install.")
    status_label.pack(anchor="w", padx=12, pady=(8, 0))

    progress = ttk.Progressbar(root, mode="indeterminate")
    progress.pack(fill="x", padx=12, pady=(4, 8))

    log_widget = scrolledtext.ScrolledText(root, height=16, state="disabled", font=("Consolas", 9))
    log_widget.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill="x", padx=12, pady=(0, 12))

    def append_log(msg: str) -> None:
        log_widget.config(state="normal")
        log_widget.insert("end", msg + "\n")
        log_widget.see("end")
        log_widget.config(state="disabled")

    def drain_queue() -> None:
        try:
            while True:
                append_log(log_queue.get_nowait())
        except queue.Empty:
            pass
        root.after(150, drain_queue)

    def set_status(text: str) -> None:
        root.after(0, lambda: status_label.config(text=text))

    def finish(success: bool) -> None:
        state["success"] = success

        def _update() -> None:
            progress.stop()
            install_btn.config(state="disabled")
            if success:
                status_label.config(text="Done.")
                launch_btn.config(state="normal")
            else:
                status_label.config(text="Install failed -- see log below.")

        root.after(0, _update)

    def worker() -> None:
        try:
            set_status("Locating uv...")
            uv_path = find_uv()
            if uv_path is None:
                set_status("Installing uv...")
                uv_path = install_uv(log)
            if uv_path is None:
                log("ERROR: could not install or locate uv.")
                finish(False)
                return
            log(f"Using uv at {uv_path}")
            state["uv_path"] = uv_path

            set_status("Installing dependencies (uv sync)... this can take several minutes")
            rc = run_streamed([uv_path, "sync"], REPO_ROOT, log)
            if rc != 0:
                log(f"ERROR: uv sync failed with exit code {rc}")
                finish(False)
                return
            log("uv sync completed successfully.")

            set_status("Creating Start Menu shortcut...")
            try:
                create_shortcut(uv_path, log)
            except Exception as exc:
                log(f"WARNING: shortcut creation failed: {exc}")

            if autostart_var.get():
                set_status("Registering autostart...")
                rc = run_streamed(
                    [uv_path, "run", "--project", str(REPO_ROOT), "scriba", "--autostart"],
                    REPO_ROOT,
                    log,
                )
                if rc != 0:
                    log(f"WARNING: enabling autostart failed with exit code {rc}")
                else:
                    log("Autostart enabled.")

            log("Install complete.")
            finish(True)
        except Exception as exc:  # keep the worker thread from dying silently
            log(f"ERROR: unexpected failure: {exc}")
            finish(False)

    def start_install() -> None:
        install_btn.config(state="disabled")
        progress.start(12)
        threading.Thread(target=worker, daemon=True).start()

    def do_launch() -> None:
        uv_path = state.get("uv_path") or find_uv()
        if uv_path:
            try:
                subprocess.Popen(
                    [uv_path, "run", "--project", str(REPO_ROOT), "scriba"],
                    cwd=str(REPO_ROOT),
                    creationflags=(
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | getattr(subprocess, "DETACHED_PROCESS", 0)
                    )
                    if is_windows()
                    else 0,
                )
                log("Launched Scriba.")
            except Exception as exc:
                messagebox.showerror("Scriba Setup", f"Could not launch Scriba: {exc}")
        root.destroy()

    install_btn = ttk.Button(btn_frame, text="Install", command=start_install)
    install_btn.pack(side="left")
    if warnings:
        install_btn.config(state="disabled")

    launch_btn = ttk.Button(
        btn_frame, text="Launch Scriba now", command=do_launch, state="disabled"
    )
    launch_btn.pack(side="left", padx=(8, 0))

    ttk.Button(btn_frame, text="Close", command=root.destroy).pack(side="right")

    root.after(150, drain_queue)
    root.mainloop()

    if state["success"] is None:
        return 1  # window closed before completion
    return 0 if state["success"] else 1


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ScribaSetup", description="Scriba installer")
    parser.add_argument(
        "--unattended",
        "--silent",
        action="store_true",
        dest="unattended",
        help="run without a GUI using sensible defaults; scriptable (exit 0 on success)",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="register Scriba to start at login (unattended mode only; the GUI has its own "
        "checkbox, default unchecked)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_arg_parser().parse_args(normalize_argv(argv))

    if args.unattended:
        return run_unattended(args)
    return run_gui(args)


def _selftest() -> None:
    assert parse_nvidia_smi_csv("NVIDIA GeForce RTX 3050 4GB Laptop GPU, 4096 MiB\n") == [
        ("NVIDIA GeForce RTX 3050 4GB Laptop GPU", 4096)
    ], "basic single-GPU csv line"
    assert parse_nvidia_smi_csv("") == [], "empty output"
    assert parse_nvidia_smi_csv("garbage line without a comma") == [], "malformed line ignored"
    assert parse_nvidia_smi_csv("A, 1024 MiB\nB, 8192 MiB\n") == [
        ("A", 1024),
        ("B", 8192),
    ], "multi-GPU"

    assert gpu_warnings([]) != [], "no GPU should warn"
    assert gpu_warnings([("RTX 3050", 4096)], min_vram_mb=3500) == [], "enough VRAM, no warning"
    assert gpu_warnings([("Old GPU", 2048)], min_vram_mb=3500) != [], "too little VRAM warns"

    assert normalize_argv(["/S"]) == ["--unattended"]
    assert normalize_argv(["/silent", "--autostart"]) == ["--unattended", "--autostart"]
    assert normalize_argv(["--unattended"]) == ["--unattended"], "already-canonical flag untouched"

    assert str(local_appdata_dir()).endswith("Scriba")
    assert str(install_log_path()).endswith(os.path.join("Scriba", "logs", "install.log"))
    assert str(start_menu_shortcut_path()).endswith("Scriba.lnk")

    parser = build_arg_parser()
    ns = parser.parse_args(normalize_argv(["/S", "--autostart"]))
    assert ns.unattended is True
    assert ns.autostart is True

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
