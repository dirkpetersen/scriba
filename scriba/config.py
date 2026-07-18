"""Load/create/validate %APPDATA%\\Scriba\\config.toml (DESIGN.md §7.9)."""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

import tomlkit


class ConfigError(ValueError):
    """Raised for a missing/invalid config.toml value."""


def config_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Scriba"


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Scriba"


def config_path() -> Path:
    return config_dir() / "config.toml"


def vocabulary_path() -> Path:
    return config_dir() / "vocabulary.txt"


def models_dir() -> Path:
    return data_dir() / "models"


def logs_dir() -> Path:
    return data_dir() / "logs"


def adaptation_dir() -> Path:
    return data_dir() / "adaptation"


_GENERAL_MODES = {"push_to_talk", "toggle", "wake_word"}
_LANGUAGES = {"en", "de", "auto", "mixed"}
_STT_DEVICES = {"auto", "cuda", "cpu"}
_STT_BACKENDS = {"local", "aws"}
_INJECT_METHODS = {"type", "paste"}
_STREAMING_POLICIES = {"eager", "stable"}


@dataclass
class GeneralConfig:
    mode: str = "toggle"
    language: str = "en"


@dataclass
class AudioConfig:
    enabled_devices: list[str] = field(default_factory=list)
    device_priority: list[str] = field(default_factory=list)


@dataclass
class VadConfig:
    threshold: float = 0.5
    endpoint_silence_ms: int = 600
    pre_roll_ms: int = 400
    min_speech_ms: int = 250
    max_utterance_s: int = 30


@dataclass
class WakeWordConfig:
    model: str = "hey_jarvis"
    threshold: float = 0.6
    sleep_phrase: str = "stop listening"
    auto_sleep_s: int = 15
    sounds: bool = True


@dataclass
class SttConfig:
    backend: str = "local"
    # Derived from general.language at every ScribaApp construction (see
    # scriba.stt.language.model_for_language) -- not independently
    # meaningful; this default just matches general.language's own default.
    model: str = "distil-large-v3"
    device: str = "auto"
    compute_type: str = "int8_float16"
    beam_size: int = 1
    languages: list[str] = field(default_factory=lambda: ["en", "de"])
    language_confidence_min: float = 0.6
    initial_prompt: str = ""
    # Unload the model (freeing ~1.35GB host RAM CTranslate2 keeps resident
    # alongside the GPU copy -- see DESIGN.md §9) after this many idle
    # minutes with dictation disabled; reloads on next activation. 0 disables
    # idle-unload (model stays resident forever once loaded).
    idle_unload_minutes: int = 60
    # Spectral-gating background-noise suppression on the audio buffer right
    # before each decode pass (user request: background noise hurt accuracy
    # noticeably more than it does for Windows' own dictation). ~20-60ms
    # overhead per decode on this machine, negligible against the latency
    # budget (DESIGN §6). Default OFF: at full strength this measurably
    # gutted real speech (a VAD-trimmed utterance buffer is almost all
    # signal, with no long quiet stretch for the algorithm to calibrate
    # against, so it over-gates) -- confirmed live, dictation produced
    # nothing at all, even shouting. Kept opt-in until tuned further.
    denoise: bool = False


@dataclass
class AdaptationConfig:
    enabled: bool = False


@dataclass
class StreamingConfig:
    enabled: bool = True
    policy: str = "eager"
    interval_ms: int = 800
    window_s: int = 15


@dataclass
class PostprocConfig:
    filler_removal: bool = True
    umlaut_fold: bool = False
    correction_threshold: int = 87
    blocklist_extra: list[str] = field(default_factory=list)


@dataclass
class InjectConfig:
    method: str = "type"
    per_char_delay_ms: int = 2
    per_app: dict[str, str] = field(default_factory=dict)


@dataclass
class HotkeysConfig:
    toggle: str = "ctrl+alt+d"
    push_to_talk: str = "ctrl+alt+space"
    language_switch: str = "ctrl+alt+l"


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    adaptation: AdaptationConfig = field(default_factory=AdaptationConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    postproc: PostprocConfig = field(default_factory=PostprocConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)


_SECTION_TYPES: dict[str, type] = {
    "general": GeneralConfig,
    "audio": AudioConfig,
    "vad": VadConfig,
    "wake_word": WakeWordConfig,
    "stt": SttConfig,
    "adaptation": AdaptationConfig,
    "streaming": StreamingConfig,
    "postproc": PostprocConfig,
    "inject": InjectConfig,
    "hotkeys": HotkeysConfig,
}


def _build_section(section_cls: type, raw: dict) -> object:
    valid_names = {f.name for f in fields(section_cls)}
    unknown = set(raw) - valid_names
    if unknown:
        raise ConfigError(f"unknown key(s) {sorted(unknown)} in [{section_cls.__name__}]")
    return section_cls(**raw)


def _validate(config: Config) -> None:
    if config.general.mode not in _GENERAL_MODES:
        raise ConfigError(
            f"general.mode must be one of {_GENERAL_MODES}, got {config.general.mode!r}"
        )
    if config.general.language not in _LANGUAGES:
        raise ConfigError(
            f"general.language must be one of {_LANGUAGES}, got {config.general.language!r}"
        )
    if not 0.0 < config.vad.threshold < 1.0:
        raise ConfigError(f"vad.threshold must be in (0, 1), got {config.vad.threshold}")
    if config.vad.endpoint_silence_ms <= 0:
        raise ConfigError("vad.endpoint_silence_ms must be positive")
    if config.vad.pre_roll_ms < 0:
        raise ConfigError("vad.pre_roll_ms must be >= 0")
    if config.vad.min_speech_ms < 0:
        raise ConfigError("vad.min_speech_ms must be >= 0")
    if config.vad.max_utterance_s <= 0:
        raise ConfigError("vad.max_utterance_s must be positive")
    if not 0.0 < config.wake_word.threshold < 1.0:
        raise ConfigError(
            f"wake_word.threshold must be in (0, 1), got {config.wake_word.threshold}"
        )
    if config.wake_word.auto_sleep_s <= 0:
        raise ConfigError("wake_word.auto_sleep_s must be positive")
    if config.stt.backend not in _STT_BACKENDS:
        raise ConfigError(
            f"stt.backend must be one of {_STT_BACKENDS}, got {config.stt.backend!r}"
        )
    if config.stt.device not in _STT_DEVICES:
        raise ConfigError(f"stt.device must be one of {_STT_DEVICES}, got {config.stt.device!r}")
    if config.stt.beam_size < 1:
        raise ConfigError("stt.beam_size must be >= 1")
    if not config.stt.languages:
        raise ConfigError("stt.languages must not be empty")
    if not 0.0 < config.stt.language_confidence_min < 1.0:
        raise ConfigError("stt.language_confidence_min must be in (0, 1)")
    if config.stt.idle_unload_minutes < 0:
        raise ConfigError("stt.idle_unload_minutes must be >= 0")
    if config.streaming.policy not in _STREAMING_POLICIES:
        raise ConfigError(
            f"streaming.policy must be one of {_STREAMING_POLICIES}, "
            f"got {config.streaming.policy!r}"
        )
    if config.streaming.interval_ms <= 0:
        raise ConfigError("streaming.interval_ms must be positive")
    if config.streaming.window_s <= 0:
        raise ConfigError("streaming.window_s must be positive")
    if not 0 <= config.postproc.correction_threshold <= 100:
        raise ConfigError("postproc.correction_threshold must be in [0, 100]")
    if config.inject.method not in _INJECT_METHODS:
        raise ConfigError(
            f"inject.method must be one of {_INJECT_METHODS}, got {config.inject.method!r}"
        )
    if config.inject.per_char_delay_ms < 0:
        raise ConfigError("inject.per_char_delay_ms must be >= 0")
    for exe, method in config.inject.per_app.items():
        if method not in _INJECT_METHODS:
            raise ConfigError(
                f"inject.per_app[{exe!r}] must be one of {_INJECT_METHODS}, got {method!r}"
            )


def config_from_dict(raw: dict) -> Config:
    unknown = set(raw) - set(_SECTION_TYPES)
    if unknown:
        raise ConfigError(f"unknown config section(s): {sorted(unknown)}")
    kwargs = {}
    for section_name, section_cls in _SECTION_TYPES.items():
        section_raw = raw.get(section_name, {})
        if not isinstance(section_raw, dict):
            raise ConfigError(f"[{section_name}] must be a table")
        kwargs[section_name] = _build_section(section_cls, section_raw)
    config = Config(**kwargs)
    _validate(config)
    return config


DEFAULT_TOML = """\
[general]
mode = "toggle"                # push_to_talk | toggle | wake_word
language = "en"                # en | de | auto | mixed

[audio]
enabled_devices = []           # empty = all input devices
device_priority = []           # optional ordered list of preferred device names

[vad]
threshold = 0.5
endpoint_silence_ms = 600
pre_roll_ms = 400
min_speech_ms = 250
max_utterance_s = 30

[wake_word]
model = "hey_jarvis"           # until custom "hey scriba" is trained
threshold = 0.6
sleep_phrase = "stop listening"
auto_sleep_s = 15
sounds = true

[stt]
backend = "local"              # local | aws (future)
model = "distil-large-v3"      # tied to general.language at startup, see DESIGN §3
device = "auto"                # auto | cuda | cpu
compute_type = "int8_float16"
beam_size = 1
languages = ["en", "de"]       # candidate set for language = "mixed"
language_confidence_min = 0.6  # below this, fall back to languages[0]
initial_prompt = ""            # optional decoder priming, see DESIGN §7.10(a)
idle_unload_minutes = 60       # unload model to free host RAM after this long disabled; 0 = never
denoise = false                # background-noise suppression, opt-in

[adaptation]
enabled = false                # "flag last utterance" accent flywheel, DESIGN §7.10(d)

[streaming]
enabled = true                 # Windows-parity partials, DESIGN §7.4a
policy = "eager"               # eager | stable
interval_ms = 800
window_s = 15

[postproc]
filler_removal = true
umlaut_fold = false
correction_threshold = 87
blocklist_extra = []

[inject]
method = "type"                # type | paste
per_char_delay_ms = 2

[inject.per_app]               # exe name -> method override
# "someapp.exe" = "paste"

[hotkeys]
toggle = "ctrl+alt+d"
push_to_talk = "ctrl+alt+space"
language_switch = "ctrl+alt+l"
"""


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML, encoding="utf-8")
        return config_from_dict(tomlkit.parse(DEFAULT_TOML).unwrap())
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return config_from_dict(raw)


def save_config(config: Config, path: Path | None = None) -> None:
    """Round-trips through the existing file's tomlkit document to preserve comments/formatting."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.parse(DEFAULT_TOML)
    for section_name in _SECTION_TYPES:
        section = getattr(config, section_name)
        doc[section_name] = {f.name: getattr(section, f.name) for f in fields(section)}
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
