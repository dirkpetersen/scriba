"""Global hotkeys via the `keyboard` package (DESIGN.md §7.8).

`keyboard.add_hotkey` parses combo strings like `"ctrl+alt+d"` natively, so
`HotkeysConfig`'s values are used as-is -- no custom parser. Push-to-talk hold
semantics come from `add_hotkey`'s own `trigger_on_release` flag: the same
combo is registered twice (once firing on press, once on release) rather than
hand-rolling key-down/key-up tracking.
"""

from collections.abc import Callable
from enum import Enum

import keyboard

from ..config import HotkeysConfig


class HotkeyAction(Enum):
    TOGGLE = "toggle"
    PUSH_TO_TALK_DOWN = "push_to_talk_down"
    PUSH_TO_TALK_UP = "push_to_talk_up"
    LANGUAGE_SWITCH = "language_switch"


# action -> (HotkeysConfig attribute name, trigger_on_release)
_HOTKEY_SPEC: dict[HotkeyAction, tuple[str, bool]] = {
    HotkeyAction.TOGGLE: ("toggle", False),
    HotkeyAction.PUSH_TO_TALK_DOWN: ("push_to_talk", False),
    HotkeyAction.PUSH_TO_TALK_UP: ("push_to_talk", True),
    HotkeyAction.LANGUAGE_SWITCH: ("language_switch", False),
}


class HotkeyManager:
    """Registers global hotkeys read from `HotkeysConfig` and dispatches per-action callbacks.

    Usage:
        manager = HotkeyManager(config.hotkeys)
        manager.register(HotkeyAction.TOGGLE, on_toggle)
        manager.register(HotkeyAction.PUSH_TO_TALK_DOWN, on_ptt_down)
        manager.register(HotkeyAction.PUSH_TO_TALK_UP, on_ptt_up)
        manager.start()
        ...
        manager.stop()

    The caller supplies plain callbacks per logical action; it never needs to
    know the configured key strings.
    """

    def __init__(self, config: HotkeysConfig) -> None:
        self._config = config
        self._callbacks: dict[HotkeyAction, Callable[[], None]] = {}
        self._handlers: list[object] = []
        self._started = False

    def register(self, action: HotkeyAction, callback: Callable[[], None]) -> None:
        self._callbacks[action] = callback
        if self._started:
            self._activate(action, callback)

    def start(self) -> None:
        self._started = True
        for action, callback in self._callbacks.items():
            self._activate(action, callback)

    def stop(self) -> None:
        # Remove by handler object, not hotkey string: push-to-talk registers
        # the same combo twice (press + release variants). `keyboard` keeps a
        # single string->remover slot that the second registration overwrites
        # (see its own "TODO: allow multiple callbacks" comment), so removing
        # by string risks tearing down the wrong registration.
        #
        # Even removing by handler, `keyboard`'s own bookkeeping has a bug for
        # this exact double-registration: each handler's remove_() always
        # unhooks the real OS-level key handler *first* (the part that
        # matters), then tries to delete three dict entries keyed by hotkey
        # string/handler/callback -- and the hotkey-string entry was already
        # deleted by whichever handler was removed first, so the second
        # removal raises KeyError *after* its unhook already succeeded. Safe
        # to swallow.
        for handler in self._handlers:
            try:
                keyboard.remove_hotkey(handler)
            except KeyError:
                pass
        self._handlers.clear()
        self._started = False

    def _activate(self, action: HotkeyAction, callback: Callable[[], None]) -> None:
        attr, trigger_on_release = _HOTKEY_SPEC[action]
        hotkey_str = getattr(self._config, attr)
        handler = keyboard.add_hotkey(hotkey_str, callback, trigger_on_release=trigger_on_release)
        self._handlers.append(handler)
