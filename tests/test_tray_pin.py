"""Real Windows-registry integration test (not mocked): creates a throwaway
NotifyIconSettings-shaped key under a private registry root, points
pin_tray_icon's exe-path matching at it using this process's own real image
path (`_current_process_image_path()` -- NOT `_current_process_image_path()`, which under
`uv run` differs from what Windows actually records; see tray_pin.py's
docstring), and verifies against the ACTUAL winreg APIs this module uses.
"""

import winreg

import pytest

from scriba.ui.tray_pin import (
    _NOTIFY_ICON_SETTINGS_KEY,
    _current_process_image_path,
    pin_tray_icon,
)

_TEST_ROOT = r"Software\ScribaTrayPinTest"


@pytest.fixture
def fake_notify_icon_settings(monkeypatch):
    # Point the module at a private, disposable registry location instead of
    # the real HKCU\Control Panel\NotifyIconSettings.
    monkeypatch.setattr("scriba.ui.tray_pin._NOTIFY_ICON_SETTINGS_KEY", _TEST_ROOT)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _TEST_ROOT):
        pass
    yield
    _delete_tree(winreg.HKEY_CURRENT_USER, _TEST_ROOT)


def _delete_tree(root, path):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(root, f"{path}\\{child}")
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def _add_entry(subkey_name: str, executable_path: str, is_promoted: int | None = None):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{_TEST_ROOT}\\{subkey_name}") as key:
        winreg.SetValueEx(key, "ExecutablePath", 0, winreg.REG_SZ, executable_path)
        if is_promoted is not None:
            winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, is_promoted)


def _read_is_promoted(subkey_name: str) -> int | None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_TEST_ROOT}\\{subkey_name}") as key:
        try:
            value, _ = winreg.QueryValueEx(key, "IsPromoted")
            return value
        except FileNotFoundError:
            return None


def test_module_constant_matches_real_registry_path():
    assert _NOTIFY_ICON_SETTINGS_KEY == r"Control Panel\NotifyIconSettings"


def test_pins_the_entry_matching_this_executable(fake_notify_icon_settings):
    _add_entry("other_app", r"C:\some\other\app.exe")
    _add_entry("scriba", _current_process_image_path())

    result = pin_tray_icon()

    assert result is True
    assert _read_is_promoted("scriba") == 1
    assert _read_is_promoted("other_app") is None


def test_matching_is_case_insensitive(fake_notify_icon_settings):
    _add_entry("scriba", _current_process_image_path().upper())

    assert pin_tray_icon() is True
    assert _read_is_promoted("scriba") == 1


def test_returns_false_when_no_entry_matches(fake_notify_icon_settings):
    _add_entry("other_app", r"C:\some\other\app.exe")

    assert pin_tray_icon() is False


def test_returns_false_when_registry_key_absent(monkeypatch):
    monkeypatch.setattr(
        "scriba.ui.tray_pin._NOTIFY_ICON_SETTINGS_KEY", r"Software\ScribaTrayPinTest\DoesNotExist"
    )

    assert pin_tray_icon() is False


def test_already_promoted_entry_stays_promoted(fake_notify_icon_settings):
    _add_entry("scriba", _current_process_image_path(), is_promoted=1)

    assert pin_tray_icon() is True
    assert _read_is_promoted("scriba") == 1


def test_entry_without_executable_path_is_skipped(fake_notify_icon_settings):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{_TEST_ROOT}\\weird_entry"):
        pass  # no ExecutablePath value at all
    _add_entry("scriba", _current_process_image_path())

    assert pin_tray_icon() is True
    assert _read_is_promoted("scriba") == 1
