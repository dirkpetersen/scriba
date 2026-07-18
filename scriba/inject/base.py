"""Injector protocol shared by all injection backends (DESIGN.md §7.7).

Platform-specific implementations live in this package (`scriba/inject/`,
per CLAUDE.md's "platform-specific code stays isolated" rule); this module
itself stays a plain, OS-neutral Protocol so `scriba/text/` and callers can
depend on the shape without importing Windows-only code.
"""

from typing import Protocol

from ..messages import ForegroundWindow, InjectJob


class Injector(Protocol):
    def foreground_window(self) -> ForegroundWindow | None:
        """Query the current foreground window, or None if there is none."""
        ...

    def inject(self, job: InjectJob) -> None:
        """Erase `job.erase` characters, then type/paste `job.text`, into the foreground window.

        Raises on failure (e.g. no foreground window, or injection blocked by
        UIPI) -- see DESIGN §9. Implementations should best-effort copy
        `job.text` to the clipboard as a consolation before raising.
        """
        ...
