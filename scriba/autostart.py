"""Windows Run-key autostart registration (DESIGN.md §12)."""

import shutil
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Scriba"
_UNSET = object()


def autostart_command(repo_root: Path | None = None, uv_path=_UNSET) -> str:
    """The command line written to the Run key. Split out (pure, no winreg) for testing.

    `uv_path` defaults to a sentinel (look up via PATH); pass `uv_path=None`
    explicitly to simulate "uv not found" without needing to mock PATH.
    """
    if uv_path is _UNSET:
        uv_path = shutil.which("uv")
    if uv_path is None:
        raise RuntimeError("uv not found on PATH; cannot register autostart")
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    return f'"{uv_path}" run --project "{repo_root}" scriba'


def enable_autostart(repo_root: Path | None = None) -> None:
    import winreg

    command = autostart_command(repo_root)
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)
    finally:
        winreg.CloseKey(key)


def disable_autostart() -> None:
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.DeleteValue(key, _VALUE_NAME)
    except FileNotFoundError:
        pass
    finally:
        winreg.CloseKey(key)


def is_autostart_enabled() -> bool:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return False
    try:
        winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    finally:
        winreg.CloseKey(key)
