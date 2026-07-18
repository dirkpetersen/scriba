"""Named-mutex single-instance guard (carried over from v1, DESIGN.md M0)."""

import win32api
import win32event
from winerror import ERROR_ALREADY_EXISTS

_MUTEX_NAME = "Global\\ScribaSingleInstanceMutex"


class SingleInstance:
    """Holds the named mutex for the process lifetime; `already_running` tells the caller to exit.

    Usage:
        guard = SingleInstance()
        if guard.already_running:
            sys.exit(0)
        ...
        guard.release()  # or let process exit close the handle
    """

    def __init__(self) -> None:
        self._handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
        self.already_running = win32api.GetLastError() == ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle is not None:
            win32api.CloseHandle(self._handle)
            self._handle = None
