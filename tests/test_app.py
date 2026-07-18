"""Unit tests for the idle-unload / reload / model-switch state machine in
scriba/app.py, using a fake STT backend so nothing here touches a real GPU
or model download. AudioCapture/Detector/WindowsInjector/HotkeyManager are
used for real -- their constructors are side-effect-free (device/hook I/O
only happens in .start(), which these tests never call).
"""

from unittest.mock import patch

import pytest

from scriba.config import Config


class _SyncThread:
    """Stand-in for threading.Thread that runs its target immediately, in-line.

    `_reload_backend()`/`_provision_worker` spawn a real background thread;
    replacing it keeps these tests deterministic (no sleeps/event-loop
    pumping) while still exercising the exact same code path -- the emitted
    Qt signals resolve to direct (synchronous) calls anyway since this then
    runs on the same thread as the test itself.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _FakeGuard:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _FakeBackend:
    def __init__(self, config):
        self._config = config
        self.rung = 1
        self.descriptor = "fake/desc"
        self.load_calls = 0
        self.unload_calls = 0
        self.fail_next_load = False

    def load(self, progress_cb):
        self.load_calls += 1
        if self.fail_next_load:
            raise RuntimeError("simulated load failure")
        progress_cb(1.0, "ready")

    def unload(self):
        self.unload_calls += 1

    def transcribe(self, *args, **kwargs):
        raise NotImplementedError

    def detect_language_probs(self, pcm):
        return {}


@pytest.fixture(scope="module")
def qapp():
    pyside6 = pytest.importorskip("PySide6.QtWidgets")
    app = pyside6.QApplication.instance() or pyside6.QApplication([])
    yield app


@pytest.fixture
def app(qapp):
    from scriba.app import ScribaApp

    with (
        patch("scriba.app.WhisperLocalBackend", _FakeBackend),
        patch("scriba.app.threading.Thread", _SyncThread),
    ):
        instance = ScribaApp(Config(), _FakeGuard())
        yield instance
    instance.tray.deleteLater()


def test_enabling_before_initial_load_is_rejected(app):
    app._set_enabled(True)

    assert app.enabled is False
    assert app.tray._enable_action.isChecked() is False


def test_initial_load_success_arms_and_starts_idle_clock(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")

    assert app._initial_load_done is True
    assert app._model_loaded is True
    assert app._disabled_since is not None
    assert app.degraded is False


def test_initial_load_failure_leaves_disabled(app):
    app._on_provision_done(False, "boom")

    assert app._initial_load_done is False
    assert app._model_loaded is False


def test_enable_after_successful_load(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")

    app._set_enabled(True)

    assert app.enabled is True
    assert app._disabled_since is None
    assert app.tray._enable_action.isChecked() is True


def test_disabling_records_disabled_since(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._set_enabled(True)

    app._set_enabled(False)

    assert app.enabled is False
    assert app._disabled_since is not None


def test_idle_unload_does_not_trigger_before_timeout(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._config.stt.idle_unload_minutes = 60
    app._disabled_since = _time_ago(minutes=1)

    app._check_idle_unload()

    assert app._model_loaded is True
    assert app._backend.unload_calls == 0


def test_idle_unload_triggers_after_timeout(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._config.stt.idle_unload_minutes = 60
    app._disabled_since = _time_ago(minutes=61)

    app._check_idle_unload()

    assert app._model_loaded is False
    assert app._backend.unload_calls == 1


def test_idle_unload_disabled_when_zero(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._config.stt.idle_unload_minutes = 0
    app._disabled_since = _time_ago(minutes=999)

    app._check_idle_unload()

    assert app._model_loaded is True


def test_idle_unload_skipped_while_enabled(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._set_enabled(True)
    app._config.stt.idle_unload_minutes = 60
    app._disabled_since = _time_ago(minutes=999)  # stale value from a prior cycle

    app._check_idle_unload()

    assert app._model_loaded is True


def test_enabling_after_idle_unload_reloads_then_enables(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._model_loaded = False  # simulate a prior idle-unload

    app._set_enabled(True)  # _reload_worker runs synchronously (same thread, direct Qt connection)

    # note: the "initial load" above was simulated by calling _on_provision_done
    # directly (that's the code under test elsewhere), bypassing the real
    # _provision_worker/backend.load() call -- so this reload is backend.load()'s
    # only actual invocation here.
    assert app._backend.load_calls == 1
    assert app._backend.unload_calls == 1  # _reload_worker unloads before reloading
    assert app._model_loaded is True
    assert app.enabled is True


def test_reload_failure_does_not_enable(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._model_loaded = False
    app._backend.fail_next_load = True

    app._set_enabled(True)

    assert app.enabled is False
    assert app._model_loaded is False


def test_default_config_ties_model_to_language_at_construction(app):
    # default Config() has general.language == "en"
    assert app._config.stt.model == "distil-large-v3"


def test_language_changed_to_german_switches_to_large_model_and_reloads(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")

    with patch("scriba.app.save_config") as mock_save:
        app._on_language_changed("de")

    assert app._config.general.language == "de"
    assert app._config.stt.model == "large-v3-turbo"
    mock_save.assert_called_once_with(app._config)
    assert app._backend.load_calls == 1  # the reload triggered by the language switch
    assert app._model_loaded is True


def test_language_changed_while_enabled_re_enables_after_reload(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    app._set_enabled(True)

    with patch("scriba.app.save_config"):
        app._on_language_changed("de")

    assert app.enabled is True  # was on before the switch, so it resumes automatically


def test_language_changed_within_same_model_tier_does_not_reload(app):
    """"de" and "mixed" both require large-v3-turbo -- switching between them
    shouldn't pay for an unnecessary reload."""
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    with patch("scriba.app.save_config"):
        app._on_language_changed("de")
    loads_before = app._backend.load_calls

    with patch("scriba.app.save_config") as mock_save:
        app._on_language_changed("mixed")

    mock_save.assert_called_once_with(app._config)
    assert app._backend.load_calls == loads_before


def test_language_changed_back_to_english_switches_to_small_model(app):
    with patch.object(app, "_start_pipeline"):
        app._on_provision_done(True, "fake/desc")
    with patch("scriba.app.save_config"):
        app._on_language_changed("de")

    with patch("scriba.app.save_config"):
        app._on_language_changed("en")

    assert app._config.stt.model == "distil-large-v3"


def _time_ago(minutes: float):
    import time

    return time.monotonic() - minutes * 60
