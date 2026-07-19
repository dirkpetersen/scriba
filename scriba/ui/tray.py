"""System tray icon, state colors, and context menu (DESIGN.md §5, §7.8).

Scoped to M1: Enable/Disable, Mode, Language, Open log, Quit. Settings window,
first-run wizard, vocabulary editor, and the wake-word tab are M3 and are not
built here.
"""

import os
from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..config import GeneralConfig, logs_dir


class TrayState(Enum):
    """Runtime states per DESIGN.md §5."""

    DISABLED = "disabled"
    ARMED = "armed"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    PROVISIONING = "provisioning"
    DEGRADED = "degraded"
    ERROR = "error"


# DESIGN.md §5: gray=DISABLED, yellow=ARMED, green=LISTENING, blue=TRANSCRIBING
# (usually a blink), orange=DEGRADED, red=ERROR, animated badge=PROVISIONING.
# Plain dict of hex strings (no QColor/Qt objects) so this mapping is testable
# without a QApplication.
STATE_COLORS: dict[TrayState, str] = {
    TrayState.DISABLED: "#808080",
    TrayState.ARMED: "#e6c619",
    TrayState.LISTENING: "#2ecc40",
    TrayState.TRANSCRIBING: "#0074d9",
    TrayState.PROVISIONING: "#b10dc9",
    TrayState.DEGRADED: "#ff851b",
    TrayState.ERROR: "#ff4136",
}

STATE_LABELS: dict[TrayState, str] = {
    TrayState.DISABLED: "Disabled",
    TrayState.ARMED: "Armed",
    TrayState.LISTENING: "Listening",
    TrayState.TRANSCRIBING: "Transcribing",
    TrayState.PROVISIONING: "Provisioning…",
    TrayState.DEGRADED: "Degraded",
    TrayState.ERROR: "Error",
}

# TRANSCRIBING blinks per §5 ("usually a blink"); PROVISIONING gets the
# "animated badge" via the same blink timer since there's no separate asset.
BLINKING_STATES: frozenset[TrayState] = frozenset({TrayState.TRANSCRIBING, TrayState.PROVISIONING})

_MODE_MENU_ITEMS = [
    ("push_to_talk", "Push-to-talk"),
    ("toggle", "Toggle"),
    ("wake_word", "Wake word"),
]

_LANGUAGE_MENU_ITEMS = [
    ("en", "English"),
    ("de", "Deutsch"),
    ("mixed", "Mixed (EN+DE)"),
    ("auto", "Auto"),
]

# No user-facing Model menu: DESIGN §3 requires distil-large-v3 (English-only)
# vs. large-v3-turbo (multilingual) to be derived from the language, not
# independently selectable -- see scriba.stt.language.model_for_language.

# Sentinel for "all microphones" (matches AudioConfig.enabled_devices == []
# meaning "all enabled" -- see scriba/audio/capture.py's list_devices()).
ALL_MICROPHONES_VALUE = ""
_ALL_MICROPHONES_LABEL = "All microphones"


def state_color(state: TrayState, *, dim: bool = False) -> str:
    """Pure state -> hex color lookup; `dim` returns the blink's off-phase shade."""
    color = STATE_COLORS[state]
    return _dim_hex(color) if dim else color


def _dim_hex(hex_color: str, factor: float = 0.35) -> str:
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def blinks(state: TrayState) -> bool:
    return state in BLINKING_STATES


def make_icon(state: TrayState, *, dim: bool = False, size: int = 64) -> QIcon:
    """Render a flat colored-dot icon for `state` (no per-state asset exists yet)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(state_color(state, dim=dim)))
        painter.setPen(Qt.PenStyle.NoPen)
        margin = size * 0.08
        diameter = size - 2 * margin
        painter.drawEllipse(int(margin), int(margin), int(diameter), int(diameter))
    finally:
        painter.end()
    return QIcon(pixmap)


class ScribaTray(QSystemTrayIcon):
    """`QSystemTrayIcon` wrapper: state-driven icon/tooltip plus the M1 context menu.

    User actions surface as Qt signals so a caller (the future `app.py`) can
    wire them up without this class knowing about the pipeline:
    `enabled_changed(bool)`, `mode_changed(str)`, `language_changed(str)`,
    `quit_requested()`. Feed pipeline state back in via `set_state()` and
    `update_status()`.
    """

    enabled_changed = Signal(bool)
    mode_changed = Signal(str)
    language_changed = Signal(str)
    microphone_changed = Signal(str)
    quit_requested = Signal()

    def __init__(
        self,
        general: GeneralConfig | None = None,
        parent=None,
        microphone_devices: list[str] | None = None,
        current_microphone: str = ALL_MICROPHONES_VALUE,
    ) -> None:
        general = general or GeneralConfig()
        self._state = TrayState.DISABLED
        self._blink_on = True
        self._model_descriptor = ""
        self._language_descriptor = ""

        super().__init__(make_icon(self._state), parent)

        self._menu = QMenu()
        self._enable_action = self._build_enable_action(self._menu)
        self._menu.addSeparator()
        self._mode_group, self._mode_actions = self._build_exclusive_submenu(
            self._menu, "Mode", _MODE_MENU_ITEMS, general.mode, self._on_mode_triggered
        )
        self._language_group, self._language_actions = self._build_exclusive_submenu(
            self._menu,
            "Language",
            _LANGUAGE_MENU_ITEMS,
            general.language,
            self._on_language_triggered,
        )
        self._microphone_submenu = self._menu.addMenu("Microphone")
        self._microphone_group: QActionGroup | None = None
        self._microphone_actions: dict[str, QAction] = {}
        self._microphone_value = current_microphone
        self.set_microphone_devices(microphone_devices or [], current_microphone)
        self._menu.addSeparator()
        open_log_action = QAction("Open log", self._menu)
        open_log_action.triggered.connect(self._open_log_dir)
        self._menu.addAction(open_log_action)
        self._menu.addSeparator()
        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_action)
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)

        self._blink_timer = QTimer()
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._on_blink_tick)
        self._blink_timer.start()

        self._update_tooltip()

    def _build_enable_action(self, menu: QMenu) -> QAction:
        action = QAction("Enabled", menu, checkable=True)
        action.setChecked(False)
        action.toggled.connect(self.enabled_changed.emit)
        menu.addAction(action)
        return action

    def _build_exclusive_submenu(
        self,
        menu: QMenu,
        title: str,
        items: list[tuple[str, str]],
        current_value: str,
        on_triggered: Callable[[str], None],
    ) -> tuple[QActionGroup, dict[str, QAction]]:
        submenu = menu.addMenu(title)
        group = QActionGroup(submenu)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        for value, label in items:
            action = QAction(label, submenu, checkable=True)
            action.setChecked(value == current_value)
            action.triggered.connect(lambda checked=False, v=value: on_triggered(v))
            group.addAction(action)
            submenu.addAction(action)
            actions[value] = action
        return group, actions

    def _on_mode_triggered(self, value: str) -> None:
        self.mode_changed.emit(value)

    def _on_language_triggered(self, value: str) -> None:
        self.language_changed.emit(value)

    def _on_microphone_triggered(self, value: str) -> None:
        self.microphone_changed.emit(value)

    def set_microphone_devices(self, names: list[str], current: str | None = None) -> None:
        """(Re)builds the Microphone submenu from the current device list.

        Called again whenever hot-plug changes what's available (devices are
        dynamic, unlike Mode/Language) -- rebuilds from scratch rather than
        diffing, since this is just a handful of menu actions.
        """
        if current is not None:
            self._microphone_value = current
        self._microphone_submenu.clear()
        group = QActionGroup(self._microphone_submenu)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        items = [(ALL_MICROPHONES_VALUE, _ALL_MICROPHONES_LABEL)] + [(n, n) for n in names]
        for value, label in items:
            action = QAction(label, self._microphone_submenu, checkable=True)
            action.setChecked(value == self._microphone_value)
            action.triggered.connect(
                lambda checked=False, v=value: self._on_microphone_triggered(v)
            )
            group.addAction(action)
            self._microphone_submenu.addAction(action)
            actions[value] = action
        self._microphone_group = group
        self._microphone_actions = actions

    def set_microphone_checked(self, name: str) -> None:
        self._microphone_value = name
        action = self._microphone_actions.get(name)
        if action is not None:
            action.setChecked(True)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._enable_action.toggle()

    def _open_log_dir(self) -> None:
        path = logs_dir()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # noqa: S606 -- Windows-only UI code, by design

    @property
    def state(self) -> TrayState:
        return self._state

    def set_state(self, state: TrayState) -> None:
        self._state = state
        self._blink_on = True
        self._update_icon()
        self._update_tooltip()

    def set_enabled_checked(self, enabled: bool) -> None:
        """Update the Enable checkbox from external state without re-emitting `enabled_changed`."""
        self._enable_action.blockSignals(True)
        self._enable_action.setChecked(enabled)
        self._enable_action.blockSignals(False)

    def set_mode_checked(self, mode: str) -> None:
        action = self._mode_actions.get(mode)
        if action is not None:
            action.setChecked(True)

    def set_language_checked(self, language: str) -> None:
        action = self._language_actions.get(language)
        if action is not None:
            action.setChecked(True)

    def update_status(self, *, model: str | None = None, language: str | None = None) -> None:
        """Push free-text status (e.g. active model descriptor) into the tooltip."""
        if model is not None:
            self._model_descriptor = model
        if language is not None:
            self._language_descriptor = language
        self._update_tooltip()

    def _update_icon(self) -> None:
        dim = blinks(self._state) and not self._blink_on
        self.setIcon(make_icon(self._state, dim=dim))

    def _update_tooltip(self) -> None:
        parts = [STATE_LABELS[self._state]]
        if self._model_descriptor:
            parts.append(self._model_descriptor)
        if self._language_descriptor:
            parts.append(self._language_descriptor)
        self.setToolTip(" · ".join(parts))

    def _on_blink_tick(self) -> None:
        if not blinks(self._state):
            if not self._blink_on:
                self._blink_on = True
                self._update_icon()
            return
        self._blink_on = not self._blink_on
        self._update_icon()
