import pytest

from scriba.config import GeneralConfig
from scriba.ui.tray import STATE_COLORS, STATE_LABELS, TrayState, blinks, state_color


def test_every_state_has_a_color_and_label():
    for state in TrayState:
        assert state in STATE_COLORS
        assert state in STATE_LABELS


def test_state_color_matches_design_doc():
    # DESIGN.md §5: gray/yellow/green/blue/orange/red + animated badge.
    assert state_color(TrayState.DISABLED) == "#808080"
    assert state_color(TrayState.ARMED) == "#e6c619"
    assert state_color(TrayState.LISTENING) == "#2ecc40"
    assert state_color(TrayState.TRANSCRIBING) == "#0074d9"
    assert state_color(TrayState.DEGRADED) == "#ff851b"
    assert state_color(TrayState.ERROR) == "#ff4136"


def test_dim_variant_differs_from_base_color():
    for state in TrayState:
        assert state_color(state, dim=True) != state_color(state, dim=False)


def test_only_transcribing_and_provisioning_blink():
    assert blinks(TrayState.TRANSCRIBING)
    assert blinks(TrayState.PROVISIONING)
    assert not blinks(TrayState.DISABLED)
    assert not blinks(TrayState.ARMED)
    assert not blinks(TrayState.LISTENING)
    assert not blinks(TrayState.DEGRADED)
    assert not blinks(TrayState.ERROR)


@pytest.fixture(scope="module")
def qapp():
    pyside6 = pytest.importorskip("PySide6.QtWidgets")
    app = pyside6.QApplication.instance() or pyside6.QApplication([])
    yield app


@pytest.fixture
def tray(qapp):
    from scriba.ui.tray import ScribaTray

    instance = ScribaTray(GeneralConfig(mode="toggle", language="en"))
    yield instance
    instance.deleteLater()


def test_initial_state_is_disabled(tray):
    assert tray.state == TrayState.DISABLED
    assert tray.toolTip() == "Disabled"


def test_set_state_updates_tooltip(tray):
    tray.set_state(TrayState.LISTENING)
    assert tray.state == TrayState.LISTENING
    assert tray.toolTip() == "Listening"


def test_update_status_appends_to_tooltip(tray):
    tray.update_status(model="large-v3-turbo/int8_float16/cuda", language="en")
    assert "large-v3-turbo/int8_float16/cuda" in tray.toolTip()
    assert "en" in tray.toolTip()


def test_mode_and_language_menus_reflect_config(tray):
    assert tray._mode_actions["toggle"].isChecked()
    assert not tray._mode_actions["push_to_talk"].isChecked()
    assert tray._language_actions["en"].isChecked()
    assert not tray._language_actions["de"].isChecked()


def test_left_click_toggles_enabled_and_emits_signal(tray):
    seen = []
    tray.enabled_changed.connect(seen.append)

    tray._on_activated(tray.ActivationReason.Trigger)
    assert seen == [True]

    tray._on_activated(tray.ActivationReason.Trigger)
    assert seen == [True, False]


def test_mode_menu_selection_emits_mode_changed(tray):
    seen = []
    tray.mode_changed.connect(seen.append)

    tray._mode_actions["push_to_talk"].trigger()

    assert seen == ["push_to_talk"]


def test_language_menu_selection_emits_language_changed(tray):
    seen = []
    tray.language_changed.connect(seen.append)

    tray._language_actions["de"].trigger()

    assert seen == ["de"]


def test_quit_action_emits_quit_requested(tray):
    seen = []
    tray.quit_requested.connect(lambda: seen.append(True))

    quit_action = next(a for a in tray._menu.actions() if a.text() == "Quit")
    quit_action.trigger()

    assert seen == [True]


def test_set_enabled_checked_does_not_reemit_signal(tray):
    seen = []
    tray.enabled_changed.connect(seen.append)

    tray.set_enabled_checked(True)

    assert seen == []
    assert tray._enable_action.isChecked() is True


def test_set_mode_checked_updates_menu_without_signal(tray):
    seen = []
    tray.mode_changed.connect(seen.append)

    tray.set_mode_checked("wake_word")

    assert seen == []
    assert tray._mode_actions["wake_word"].isChecked()


