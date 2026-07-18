"""Win32 SendInput / paste injector (DESIGN.md §7.7).

Primary method is `SendInput` with `KEYEVENTF_UNICODE` via `ctypes` (stdlib --
pywin32 doesn't conveniently expose raw SendInput with Unicode code units).
Injection is per character: astral characters (outside the BMP, e.g. emoji)
are split into a UTF-16 surrogate pair and sent as two Unicode events; `\n`
is sent as `VK_RETURN` down/up instead of a Unicode event. `job.erase`
backspaces (`VK_BACK` down/up pairs) are sent before typing `job.text`, which
is how the injector honors the streaming revision protocol (DESIGN §7.4a) and
the final post-processing reconciliation -- the diffing that produces
`job.erase` is computed upstream; this module just honors it.

Per-app overrides (`config.inject.per_app`) pick "type" (SendInput) or
"paste" (clipboard + Ctrl+V) per foreground exe name.

Failure handling (DESIGN §9): if there is no foreground window, or
`SendInput` reports fewer events queued than requested (the documented signal
for UIPI blocking an elevated target window), `inject()` raises
`InjectionBlockedError` after best-effort copying `job.text` to the clipboard
as a consolation.
"""

import ctypes
import logging
import os
import time

import win32api
import win32clipboard
import win32con
import win32gui
import win32process

from ..config import InjectConfig
from ..messages import ForegroundWindow, InjectJob

logger = logging.getLogger(__name__)

# --- ctypes SendInput plumbing ----------------------------------------------
# Standard ctypes recreation of the Win32 INPUT/KEYBDINPUT structs (MOUSEINPUT
# and HARDWAREINPUT are unused but must stay in the union so `sizeof(_Input)`
# matches the real INPUT struct -- SendInput validates the caller's cbSize
# against it and silently rejects the call otherwise).

PUL = ctypes.POINTER(ctypes.c_ulong)

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_CONTROL = 0x11
VK_V = 0x56


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


def _keybd_input(vk: int, scan: int, flags: int) -> _Input:
    event = _Input(type=INPUT_KEYBOARD)
    event.union.ki = _KeyBdInput(vk, scan, flags, 0, None)
    return event


def _send_inputs(events: list[_Input]) -> int:
    """Thin wrapper over `user32!SendInput`; returns the number of events queued."""
    n = len(events)
    array = (_Input * n)(*events)
    return ctypes.windll.user32.SendInput(n, array, ctypes.sizeof(_Input))


# --- pure helpers (unit-testable without an actual SendInput call) ---------


def char_to_code_units(ch: str) -> list[int]:
    """UTF-16 code unit(s) for one Python character.

    BMP characters yield one code unit; astral characters (outside the BMP,
    e.g. most emoji) yield a high/low surrogate pair -- `str.encode` already
    does the UTF-16 surrogate-pair math, so this just reads it back out.
    """
    encoded = ch.encode("utf-16-le")
    return [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]


def resolve_inject_method(config: InjectConfig, exe_name: str | None) -> str:
    """Resolve "type"/"paste" for `exe_name`: per-app override, else `config.method`."""
    if exe_name:
        for configured_exe, method in config.per_app.items():
            if configured_exe.lower() == exe_name.lower():
                return method
    return config.method


def backspace_event_count(erase: int) -> int:
    """Number of individual key events (down+up per backspace) needed to erase `erase` chars."""
    return max(erase, 0) * 2


def _vk_down_up(vk: int) -> list[_Input]:
    return [_keybd_input(vk, 0, 0), _keybd_input(vk, 0, KEYEVENTF_KEYUP)]


def _unicode_down_up(unit: int) -> list[_Input]:
    return [
        _keybd_input(0, unit, KEYEVENTF_UNICODE),
        _keybd_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def _char_events(ch: str) -> list[_Input]:
    if ch == "\n":
        return _vk_down_up(VK_RETURN)
    events: list[_Input] = []
    for unit in char_to_code_units(ch):
        events.extend(_unicode_down_up(unit))
    return events


def _ctrl_v_events() -> list[_Input]:
    return [
        _keybd_input(VK_CONTROL, 0, 0),
        _keybd_input(VK_V, 0, 0),
        _keybd_input(VK_V, 0, KEYEVENTF_KEYUP),
        _keybd_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
    ]


def _read_clipboard_text() -> str | None:
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def _write_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


class InjectionBlockedError(Exception):
    """Raised by `WindowsInjector.inject()` when injection could not be delivered.

    Covers both "no foreground window" and SendInput reporting fewer events
    queued than requested (UIPI blocking an elevated target, DESIGN §9). The
    caller (tray integration) is responsible for turning this into a toast;
    by the time this is raised, `job.text` has already been best-effort
    copied to the clipboard as a consolation.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WindowsInjector:
    """`Injector` implementation using Win32 SendInput / clipboard paste (DESIGN §7.7)."""

    def __init__(self, config: InjectConfig) -> None:
        self._config = config

    def foreground_window(self) -> ForegroundWindow | None:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd)
        exe_name = self._exe_name_for_hwnd(hwnd)
        return ForegroundWindow(hwnd=hwnd, title=title, exe_name=exe_name)

    @staticmethod
    def _exe_name_for_hwnd(hwnd: int) -> str:
        """Best-effort exe name for `hwnd`'s owning process; "" if it can't be resolved.

        An elevated foreground window (the classic UIPI case, DESIGN §7.7) can
        make `OpenProcess`/`GetModuleFileNameEx` fail even with the "limited
        information" access right; that's not fatal here since the caller
        still gets a usable `ForegroundWindow` with hwnd/title, just no exe
        name for per-app override matching.
        """
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                win32api.CloseHandle(handle)
            return os.path.basename(path)
        except Exception:
            logger.debug("could not resolve exe name for hwnd=%r", hwnd, exc_info=True)
            return ""

    def inject(self, job: InjectJob) -> None:
        foreground = self.foreground_window()
        if foreground is None:
            self._consolation_copy(job.text)
            raise InjectionBlockedError("no foreground window")

        method = resolve_inject_method(self._config, foreground.exe_name)
        delay_s = self._config.per_char_delay_ms / 1000

        self._send_backspaces(job.erase, job.text, foreground, delay_s)
        if method == "paste":
            self._inject_paste(job.text, foreground)
        else:
            self._inject_type(job.text, foreground, delay_s)

    def _send_backspaces(
        self, erase: int, text: str, foreground: ForegroundWindow, delay_s: float
    ) -> None:
        for _ in range(max(erase, 0)):
            self._send_or_raise(_vk_down_up(VK_BACK), text, foreground)
            if delay_s:
                time.sleep(delay_s)

    def _inject_type(self, text: str, foreground: ForegroundWindow, delay_s: float) -> None:
        for ch in text:
            self._send_or_raise(_char_events(ch), text, foreground)
            if delay_s:
                time.sleep(delay_s)

    def _inject_paste(self, text: str, foreground: ForegroundWindow) -> None:
        original = _read_clipboard_text()
        try:
            _write_clipboard_text(text)
            self._send_or_raise(_ctrl_v_events(), text, foreground)
        finally:
            time.sleep(0.1)
            if original is not None:
                _write_clipboard_text(original)

    def _send_or_raise(self, events: list[_Input], text: str, foreground: ForegroundWindow) -> None:
        sent = _send_inputs(events)
        if sent < len(events):
            self._consolation_copy(text)
            raise InjectionBlockedError(
                f"SendInput queued {sent}/{len(events)} events "
                f"(UIPI likely blocked foreground window {foreground.exe_name!r})"
            )

    @staticmethod
    def _consolation_copy(text: str) -> None:
        try:
            _write_clipboard_text(text)
        except Exception:
            logger.exception("consolation clipboard copy failed")
