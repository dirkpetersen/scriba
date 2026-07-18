"""Entry point: bootstrap, single-instance mutex, thread wiring, Qt loop (DESIGN.md §5, §8).

This module is the integration layer tying together the independently-built
pipeline stages (audio/detect/stt/text/inject/ui): each of those was written
against only the shared `scriba/messages.py` contract, so the glue that
actually threads a live utterance from microphone to injected text --
per-utterance `StreamingSession` lifecycle, the partial/final revision
protocol (backspace diffing against what's already been typed, §7.4a), and
abandoning a revision on foreground-window change -- lives here.
"""

import argparse
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import diagnose
from .audio.capture import AudioCapture
from .autostart import disable_autostart, enable_autostart
from .config import Config, config_path, load_config, save_config
from .detect.vad import Detector
from .inject.windows import InjectionBlockedError, WindowsInjector
from .logging_setup import setup_logging
from .messages import AudioChunk, AudioFrame, InjectJob, PostprocState, Transcript
from .singleinstance import SingleInstance
from .stt.language import model_for_language, resolve_language
from .stt.streaming import StreamingSession
from .stt.whisper_local import WhisperLocalBackend
from .text.pipeline import run_pipeline
from .ui.hotkeys import HotkeyAction, HotkeyManager
from .ui.tray import ScribaTray, TrayState
from .ui.tray_pin import pin_tray_icon

logger = logging.getLogger(__name__)

_CPU_FALLBACK_RUNGS = frozenset({2, 4})  # whisper_local.py's _FALLBACK_RUNGS: 2 and 4 are CPU


def _fix_cuda_dll_path() -> None:
    """Prepend the pip-installed CUDA wheels' `bin` dirs to PATH.

    Without this, ctranslate2 fails to load cublas64_12.dll/cudnn64_9.dll via
    classic `LoadLibraryW` search even though `nvidia-cublas-cu12`/
    `nvidia-cudnn-cu12` are installed in the venv (verified empirically on
    this machine while building `scriba/stt/` -- `os.add_dll_directory()`
    does not fix it, only `PATH` does). Silently no-ops if the packages
    aren't importable (e.g. on a non-Windows dev checkout).
    """
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError:
        return
    # These are PEP 420 namespace packages (no __init__.py), so __file__ is
    # None -- __path__ is the correct way to locate them.
    bin_dirs = [
        str(Path(nvidia.cublas.__path__[0]) / "bin"),
        str(Path(nvidia.cudnn.__path__[0]) / "bin"),
    ]
    os.environ["PATH"] = os.pathsep.join([*bin_dirs, os.environ.get("PATH", "")])


_MODE_HINTS = {
    "push_to_talk": "Hold ctrl+alt+space to dictate.",
    "toggle": "Press ctrl+alt+d to start/stop dictating.",
    "wake_word": "Wake-word (car mode) isn't implemented yet; press ctrl+alt+d to dictate.",
}


def _welcome_message(mode: str) -> str:
    hint = _MODE_HINTS.get(mode, _MODE_HINTS["toggle"])
    return f"Local voice dictation is running in the tray. {hint} ctrl+alt+l switches language."


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class _RevisionTracker:
    """Tracks what's been typed for the in-flight utterance (DESIGN §7.4a revision protocol).

    Computes the backspace+retype diff for each partial/final update, and
    detects a foreground-window change so revision is abandoned rather than
    backspacing into a window that never received the earlier partials.
    """

    def __init__(self) -> None:
        self.typed_text = ""
        self.start_hwnd: int | None = None
        self.abandoned = False

    def begin(self, hwnd: int | None) -> None:
        self.typed_text = ""
        self.start_hwnd = hwnd
        self.abandoned = False

    def check_focus(self, hwnd: int | None) -> bool:
        if self.abandoned:
            return False
        if hwnd != self.start_hwnd:
            self.abandoned = True
            return False
        return True

    def diff_job(self, candidate_text: str, utterance_id: int, is_final: bool) -> InjectJob:
        prefix_len = _common_prefix_len(self.typed_text, candidate_text)
        erase = len(self.typed_text) - prefix_len
        suffix = candidate_text[prefix_len:]
        self.typed_text = candidate_text
        return InjectJob(text=suffix, erase=erase, utterance_id=utterance_id, is_final=is_final)

    def clear_job(self, utterance_id: int) -> InjectJob | None:
        if not self.typed_text:
            return None
        job = InjectJob(
            text="", erase=len(self.typed_text), utterance_id=utterance_id, is_final=True
        )
        self.typed_text = ""
        return job

    def reset(self) -> None:
        self.typed_text = ""
        self.start_hwnd = None
        self.abandoned = False


class ScribaApp(QObject):
    """Owns config, the pipeline queues/threads, the tray, and hotkeys."""

    _provision_progress = Signal(float, str)
    _provision_done = Signal(bool, str)
    _reload_done = Signal(bool, str)
    _state_requested = Signal(object)  # TrayState, emitted from worker threads
    _toast_requested = Signal(str, str)
    # Hotkey callbacks fire on the `keyboard` package's hook thread; they must
    # not touch Qt objects directly (undefined behavior), so they only emit
    # this signal and the handler runs on the Qt main thread.
    _hotkey_fired = Signal(object)  # HotkeyAction

    def __init__(self, config: Config, guard: SingleInstance, is_first_run: bool = False) -> None:
        super().__init__()
        self._config = config
        # DESIGN §3: distil-large-v3 (English-only) vs. large-v3-turbo
        # (multilingual) is derived from general.language, not independently
        # configurable -- enforce it here so the invariant holds regardless
        # of how config.toml (or a hand-edit) set stt.model.
        self._config.stt.model = model_for_language(self._config.general.language)
        self._guard = guard
        self._is_first_run = is_first_run
        self._stop_event = threading.Event()
        self.enabled = False
        self.degraded = False
        # Idle-unload (user request: free the ~1.35GB CTranslate2 keeps
        # resident in host RAM alongside the GPU copy, see DESIGN.md §9,
        # after `stt.idle_unload_minutes` of dictation being off; reload on
        # next activation or explicit model switch from the tray menu).
        self._initial_load_done = False  # first-ever load succeeded at least once
        self._model_loaded = False  # currently loaded (False after idle-unload)
        self._reloading = False
        self._disabled_since: float | None = None
        self._pending_enable_after_reload = False

        self._frame_queue: queue.Queue[AudioFrame] = queue.Queue()
        self._chunk_queue: queue.Queue[AudioChunk] = queue.Queue()
        self._inject_queue: queue.Queue[InjectJob] = queue.Queue()
        self._postproc_state = PostprocState()

        self._capture = AudioCapture(config, self._frame_queue)
        self._detector = Detector(
            config, self._frame_queue, self._chunk_queue, self._capture.get_preroll
        )
        self._backend = WhisperLocalBackend(config)
        self._injector = WindowsInjector(config.inject)
        self._hotkeys = HotkeyManager(config.hotkeys)

        self.tray = ScribaTray(config.general)
        self.tray.enabled_changed.connect(self._on_enabled_changed)
        self.tray.mode_changed.connect(self._on_mode_changed)
        self.tray.language_changed.connect(self._on_language_changed)
        self.tray.quit_requested.connect(self._on_quit_requested)

        self._provision_progress.connect(self._on_provision_progress)
        self._provision_done.connect(self._on_provision_done)
        self._reload_done.connect(self._on_reload_done)
        self._state_requested.connect(self.tray.set_state)
        self._toast_requested.connect(self.tray.showMessage)
        self._hotkey_fired.connect(self._on_hotkey)

        for action in HotkeyAction:
            self._hotkeys.register(
                action, lambda a=action: self._hotkey_fired.emit(a)
            )

        self._idle_unload_timer = QTimer(self)
        self._idle_unload_timer.setInterval(60_000)
        self._idle_unload_timer.timeout.connect(self._check_idle_unload)
        self._idle_unload_timer.start()

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self.tray.show()
        # Best-effort -- pin the icon to always-visible like OneDrive/VPN
        # clients, instead of defaulting into the hidden-icons overflow
        # chevron. Windows creates the registry entry tray_pin.py needs
        # lazily and with unpredictable delay after the icon is first shown
        # (observed anywhere from several seconds to a couple of minutes on
        # this machine), so retry on an interval rather than a single
        # delayed attempt; stops itself on success or after giving up.
        self._pin_tray_attempts = 0
        self._pin_tray_timer = QTimer(self)
        self._pin_tray_timer.setInterval(5000)
        self._pin_tray_timer.timeout.connect(self._try_pin_tray_icon)
        self._pin_tray_timer.start()
        self._hotkeys.start()
        self.tray.set_state(TrayState.PROVISIONING)
        threading.Thread(target=self._provision_worker, daemon=True).start()

    def _try_pin_tray_icon(self) -> None:
        self._pin_tray_attempts += 1
        if pin_tray_icon() or self._pin_tray_attempts >= 24:  # ~2 minutes at 5s intervals
            self._pin_tray_timer.stop()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._hotkeys.stop()
        try:
            self._capture.stop()
        except Exception:
            logger.exception("error stopping audio capture")
        self._guard.release()

    def _start_pipeline(self) -> None:
        self._capture.start()
        threading.Thread(target=self._detector.run, args=(self._stop_event,), daemon=True).start()
        threading.Thread(target=self._stt_loop, args=(self._stop_event,), daemon=True).start()
        threading.Thread(target=self._inject_loop, args=(self._stop_event,), daemon=True).start()

    # --- provisioning (runs on a transient thread, DESIGN §7.4) --------

    def _provision_worker(self) -> None:
        try:
            self._backend.load(lambda frac, label: self._provision_progress.emit(frac, label))
        except Exception as exc:
            logger.exception("STT model failed to load on all fallback rungs")
            self._provision_done.emit(False, str(exc))
            return
        self._provision_done.emit(True, self._backend.descriptor)

    def _on_provision_progress(self, fraction: float, label: str) -> None:
        self.tray.set_state(TrayState.PROVISIONING)
        self.tray.update_status(model=f"{label} {fraction * 100:.0f}%")

    def _on_provision_done(self, success: bool, message: str) -> None:
        if not success:
            self.tray.set_state(TrayState.ERROR)
            self.tray.update_status(model=f"STT load failed: {message}")
            logger.error("STT backend unusable: %s", message)
            return
        self._apply_rung_effects()
        self.tray.update_status(
            model=self._backend.descriptor, language=self._config.general.language
        )
        self._initial_load_done = True
        self._model_loaded = True
        self._disabled_since = time.monotonic()  # idle clock starts counting immediately
        self._start_pipeline()
        self._refresh_idle_state()
        if self._is_first_run:
            self.tray.showMessage("Scriba", _welcome_message(self._config.general.mode))

    def _apply_rung_effects(self) -> None:
        self.degraded = self._backend.rung > 1
        if self._backend.rung in _CPU_FALLBACK_RUNGS:
            # DESIGN §7.4a: "the DEGRADED CPU fallback rungs force [streaming]
            # off automatically (CPU decode isn't fast enough to re-decode on
            # a cadence)". Rungs 2/4 are the CPU rungs (whisper_local.py).
            logger.info("STT fell back to a CPU rung; disabling streaming partials")
            self._config.streaming.enabled = False

    # --- idle-unload / reload (frees ~1.35GB host RAM when disabled a while) --

    def _check_idle_unload(self) -> None:
        minutes = self._config.stt.idle_unload_minutes
        if minutes <= 0 or not self._model_loaded or self._reloading or self.enabled:
            return
        if self._disabled_since is None or time.monotonic() - self._disabled_since < minutes * 60:
            return
        logger.info("idle %d+ min with dictation off; unloading STT model to free RAM", minutes)
        self._backend.unload()
        self._model_loaded = False
        self.tray.update_status(model="unloaded (idle)")

    def _reload_backend(self) -> None:
        """(Re)loads `self._backend` per current config, with tray PROVISIONING
        state + live progress + start/finish toasts, so it's unmistakable
        that something is happening rather than the tray looking stuck.
        Used both for reactivating after an idle-unload and for an explicit
        model switch from the tray menu."""
        if self._reloading:
            return
        self._reloading = True
        self._model_loaded = False
        self._state_requested.emit(TrayState.PROVISIONING)
        self._toast_requested.emit("Scriba", "Loading speech model…")
        threading.Thread(target=self._reload_worker, daemon=True).start()

    def _reload_worker(self) -> None:
        try:
            self._backend.unload()
            self._backend.load(lambda frac, label: self._provision_progress.emit(frac, label))
        except Exception as exc:
            logger.exception("STT model (re)load failed")
            self._reload_done.emit(False, str(exc))
            return
        self._reload_done.emit(True, self._backend.descriptor)

    def _on_reload_done(self, success: bool, message: str) -> None:
        self._reloading = False
        if not success:
            self._pending_enable_after_reload = False
            self._state_requested.emit(TrayState.ERROR)
            self.tray.update_status(model=f"reload failed: {message}")
            logger.error("STT model reload failed: %s", message)
            return
        self._model_loaded = True
        self._apply_rung_effects()
        self.tray.update_status(model=self._backend.descriptor)
        self._toast_requested.emit("Scriba", "Speech model ready")
        if self._pending_enable_after_reload:
            self._pending_enable_after_reload = False
            self._finish_enable()
        else:
            self._refresh_idle_state()

    # --- tray / hotkey callbacks (main thread) --------------------------

    def _set_enabled(self, enabled: bool) -> None:
        """Single entry point for every enable/disable path (tray click, toggle
        hotkey, PTT down/up) so the activation toast fires exactly once per
        real transition, regardless of which path triggered it. No extra
        custom beep -- the OS's own notification sound on the toast is
        already the audible cue (a second, distinct tone was redundant).

        If the model was idle-unloaded, activating instead kicks off a
        reload and defers actually enabling until it finishes."""
        if enabled == self.enabled:
            return
        if not enabled:
            self.enabled = False
            self._disabled_since = time.monotonic()
            self.tray.set_enabled_checked(False)
            self._refresh_idle_state()
            return
        if not self._initial_load_done or self._reloading:
            self.tray.set_enabled_checked(False)  # reject; keep the checkbox honest
            return
        if not self._model_loaded:
            self.tray.set_enabled_checked(False)  # not enabled yet -- reload first
            self._pending_enable_after_reload = True
            self._reload_backend()
            return
        self._finish_enable()

    def _finish_enable(self) -> None:
        self.enabled = True
        self._disabled_since = None
        self.tray.set_enabled_checked(True)
        self.tray.showMessage(
            "Scriba", "Dictation started", QSystemTrayIcon.MessageIcon.Information, 3000
        )
        self._refresh_idle_state()

    def _on_enabled_changed(self, enabled: bool) -> None:
        self._set_enabled(enabled)

    def _on_mode_changed(self, mode: str) -> None:
        self._config.general.mode = mode
        save_config(self._config)

    def _on_language_changed(self, language: str) -> None:
        self._config.general.language = language
        required_model = model_for_language(language)
        model_changed = required_model != self._config.stt.model
        self._config.stt.model = required_model
        save_config(self._config)
        self.tray.update_status(language=language)

        if not model_changed:
            return
        # DESIGN §3: distil-large-v3 is English-only -- switching to/from "en"
        # requires reloading with the model that matches the new language.
        was_enabled = self.enabled
        if was_enabled:
            self._set_enabled(False)
        self.tray.update_status(model="switching model…")
        self._pending_enable_after_reload = was_enabled
        self._reload_backend()

    def _on_quit_requested(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_hotkey(self, action: HotkeyAction) -> None:
        """Runs on the Qt main thread (queued from the keyboard hook thread)."""
        if action == HotkeyAction.TOGGLE:
            self._hotkey_toggle()
        elif action == HotkeyAction.PUSH_TO_TALK_DOWN:
            self._hotkey_ptt_down()
        elif action == HotkeyAction.PUSH_TO_TALK_UP:
            self._hotkey_ptt_up()
        elif action == HotkeyAction.LANGUAGE_SWITCH:
            self._hotkey_language_switch()

    def _hotkey_toggle(self) -> None:
        if self._config.general.mode != "toggle":
            return
        self._set_enabled(not self.enabled)

    def _hotkey_ptt_down(self) -> None:
        if self._config.general.mode != "push_to_talk":
            return
        self._set_enabled(True)

    def _hotkey_ptt_up(self) -> None:
        if self._config.general.mode != "push_to_talk":
            return
        self._set_enabled(False)
        # An utterance already in flight (VAD triggered while the key was
        # held) finishes naturally via VAD endpoint rather than being cut
        # here -- Detector has no force-endpoint hook. True instant-on-
        # release PTT would need one; out of scope for this integration pass.

    def _hotkey_language_switch(self) -> None:
        order = ["en", "de"]
        current = self._config.general.language
        next_lang = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
        self._on_language_changed(next_lang)
        self.tray.set_language_checked(next_lang)

    def _refresh_idle_state(self) -> None:
        if not self.enabled:
            self._state_requested.emit(TrayState.DISABLED)
        elif self.degraded:
            self._state_requested.emit(TrayState.DEGRADED)
        else:
            self._state_requested.emit(TrayState.ARMED)

    # --- stt thread: streaming session lifecycle + revision protocol ---

    def _stt_loop(self, stop_event: threading.Event) -> None:
        session: StreamingSession | None = None
        tracker = _RevisionTracker()
        active_utterance_id: int | None = None
        last_skipped_id: int | None = None

        def emit(transcript: Transcript) -> None:
            self._handle_transcript(transcript, tracker)

        while not stop_event.is_set():
            try:
                chunk = self._chunk_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if chunk.utterance_id != active_utterance_id:
                    if not self.enabled:
                        # only start new utterances while armed; in-flight ones finish
                        if chunk.utterance_id != last_skipped_id:
                            last_skipped_id = chunk.utterance_id
                            logger.debug(
                                "skipping utterance %d (disabled)", chunk.utterance_id
                            )
                        continue
                    foreground = self._injector.foreground_window()
                    hwnd = foreground.hwnd if foreground else None
                    probs = (
                        self._backend.detect_language_probs(chunk.pcm)
                        if self._config.general.language == "mixed"
                        else None
                    )
                    chunk.language = resolve_language(
                        self._config.general.language, probs, self._config.stt
                    )
                    active_utterance_id = chunk.utterance_id
                    logger.info(
                        "utterance %d started (device %s, language %s)",
                        chunk.utterance_id,
                        chunk.device_id,
                        chunk.language or "auto",
                    )
                    tracker.begin(hwnd)
                    session = StreamingSession(self._backend, self._config, emit=emit)
                    self._state_requested.emit(TrayState.LISTENING)

                assert session is not None
                session.feed(chunk)

                if chunk.is_final:
                    session = None
                    active_utterance_id = None
            except Exception:
                logger.exception(
                    "stt loop: error handling chunk for utterance %s", chunk.utterance_id
                )
                session = None
                active_utterance_id = None
                tracker.reset()

    def _handle_transcript(self, transcript: Transcript, tracker: _RevisionTracker) -> None:
        foreground = self._injector.foreground_window()
        hwnd = foreground.hwnd if foreground else None

        if not tracker.check_focus(hwnd):
            logger.info("foreground window changed mid-utterance; abandoning revision")
            return

        if transcript.is_partial:
            self._state_requested.emit(TrayState.LISTENING)
            text = " ".join(transcript.text.split())  # whitespace normalization only, §7.4a
            job = tracker.diff_job(text, transcript.utterance_id, is_final=False)
            if job.erase or job.text:
                self._inject_queue.put(job)
            return

        self._state_requested.emit(TrayState.TRANSCRIBING)
        jobs, next_state = run_pipeline(transcript, self._postproc_state, self._config, foreground)
        self._postproc_state = next_state

        if not jobs:
            clear = tracker.clear_job(transcript.utterance_id)
            if clear is not None:
                self._inject_queue.put(clear)
        else:
            for job in jobs:
                self._inject_queue.put(tracker.diff_job(job.text, transcript.utterance_id, True))

        tracker.reset()
        self._refresh_idle_state()

    # --- inject thread ---------------------------------------------------

    def _inject_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                job = self._inject_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._injector.inject(job)
            except InjectionBlockedError as exc:
                logger.warning("injection blocked: %s", exc.reason)
                self._toast_requested.emit("Scriba: injection blocked", exc.reason)
            except Exception:
                logger.exception("unexpected injection failure")


def main() -> int:
    parser = argparse.ArgumentParser(prog="scriba")
    parser.add_argument("--diagnose", action="store_true", help="print diagnostics and exit")
    parser.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    autostart_group = parser.add_mutually_exclusive_group()
    autostart_group.add_argument(
        "--autostart", action="store_true", help="register Scriba to start at login, then exit"
    )
    autostart_group.add_argument(
        "--no-autostart", action="store_true", help="remove Scriba from login startup, then exit"
    )
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    if args.diagnose:
        diagnose.run_diagnostics()
        return 0

    if args.autostart:
        enable_autostart()
        logger.info("autostart enabled")
        return 0

    if args.no_autostart:
        disable_autostart()
        logger.info("autostart disabled")
        return 0

    _fix_cuda_dll_path()

    guard = SingleInstance()
    if guard.already_running:
        logger.warning("Scriba is already running; exiting.")
        return 1

    is_first_run = not config_path().exists()
    config = load_config()

    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)

    scriba_app = ScribaApp(config, guard, is_first_run=is_first_run)
    scriba_app.start()

    exit_code = qt_app.exec()
    scriba_app.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
