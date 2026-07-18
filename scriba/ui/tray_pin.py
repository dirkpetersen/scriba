"""Best-effort: force the Scriba tray icon to always show (not hidden behind
the overflow chevron), matching how OneDrive/VPN clients typically behave.

Undocumented Windows mechanism (verified empirically on Windows 11):
`HKCU\\Control Panel\\NotifyIconSettings\\<uid>` holds one entry per
registered tray icon, keyed by a UID Explorer computes itself; the entry
whose `ExecutablePath` matches this process's own executable is "ours", and
setting its `IsPromoted` DWORD to 1 requests always-shown placement (this is
exactly how an already-pinned app's own entry looks on this machine). Not a
public API -- Microsoft could change or remove this in a future Windows
update -- so every failure mode here is swallowed: it only ever nudges an
icon's tray *position*, never anything functional, and once set it persists
in the registry across future launches (so one success, ever, is enough).

`sys.executable` is NOT used for the match: under `uv run`, it reports the
venv-local `.venv\\Scripts\\python.exe` launcher, but Windows records the
*real* underlying interpreter binary that launcher resolves to (confirmed:
`.venv\\Scripts\\python.exe` is effectively a link to uv's shared base
install) -- `GetModuleFileNameW(NULL, ...)` asks the OS directly for this
process's own image path and matches what Explorer recorded, verified
empirically against a real registry entry on this machine.
"""

import ctypes
import logging
import winreg

logger = logging.getLogger(__name__)

_NOTIFY_ICON_SETTINGS_KEY = r"Control Panel\NotifyIconSettings"


def _current_process_image_path() -> str:
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    return buf.value


def pin_tray_icon() -> bool:
    """Sets IsPromoted=1 on this process's tray icon registry entry, if found.

    Call after the tray icon has been shown at least once (Windows creates
    the entry lazily on first registration, so this can legitimately find
    nothing on a brand new install -- a later launch will succeed instead).
    Never raises.
    """
    exe_path = _current_process_image_path().lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _NOTIFY_ICON_SETTINGS_KEY) as root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(
                        root, subkey_name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
                    ) as subkey:
                        entry_path, _ = winreg.QueryValueEx(subkey, "ExecutablePath")
                        if entry_path.lower() != exe_path:
                            continue
                        winreg.SetValueEx(subkey, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        logger.info(
                            "pinned tray icon to always-visible (registry key %s)", subkey_name
                        )
                        return True
                except OSError:
                    continue
    except OSError:
        logger.debug("could not pin tray icon (undocumented registry mechanism)", exc_info=True)
        return False
    logger.debug("tray icon registry entry not found yet (will retry next launch)")
    return False
