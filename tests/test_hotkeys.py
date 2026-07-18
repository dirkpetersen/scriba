"""Real OS-level keyboard hooks are inherently hard to unit-test; these only
check that registration/dispatch/cleanup don't raise and don't leak handlers.
No actual key events are simulated (avoids sending real input to the desktop
running the tests).
"""

from scriba.config import HotkeysConfig
from scriba.ui.hotkeys import HotkeyAction, HotkeyManager


def test_register_start_stop_does_not_raise():
    manager = HotkeyManager(HotkeysConfig())
    manager.register(HotkeyAction.TOGGLE, lambda: None)
    manager.register(HotkeyAction.PUSH_TO_TALK_DOWN, lambda: None)
    manager.register(HotkeyAction.PUSH_TO_TALK_UP, lambda: None)
    manager.register(HotkeyAction.LANGUAGE_SWITCH, lambda: None)

    manager.start()
    try:
        assert len(manager._handlers) == 4
    finally:
        manager.stop()

    assert manager._handlers == []


def test_stop_without_start_does_not_raise():
    manager = HotkeyManager(HotkeysConfig())
    manager.stop()


def test_register_after_start_activates_immediately():
    manager = HotkeyManager(HotkeysConfig())
    manager.start()
    try:
        manager.register(HotkeyAction.TOGGLE, lambda: None)
        assert len(manager._handlers) == 1
    finally:
        manager.stop()


def test_push_to_talk_down_and_up_share_hotkey_string_but_distinct_handlers():
    manager = HotkeyManager(HotkeysConfig(push_to_talk="ctrl+alt+space"))
    manager.register(HotkeyAction.PUSH_TO_TALK_DOWN, lambda: None)
    manager.register(HotkeyAction.PUSH_TO_TALK_UP, lambda: None)

    manager.start()
    try:
        assert len(manager._handlers) == 2
        assert manager._handlers[0] is not manager._handlers[1]
    finally:
        manager.stop()
