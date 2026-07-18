"""`WhisperLocalBackend` - the only v1 `SttBackend` implementation (DESIGN.md §7.4).

Model provisioning deliberately does not use `faster_whisper.utils.download_model()`:
it hardcodes `tqdm_class=disabled_tqdm` internally, so there is no way to get
download progress out of it. Instead this module resolves the HF repo id the
same way that function does (via `faster_whisper.utils._MODELS`) and calls
`huggingface_hub.snapshot_download()` directly with a custom `tqdm_class` that
drives `progress_cb`.
"""

import logging
from collections.abc import Callable
from pathlib import Path

import huggingface_hub
import noisereduce as nr
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.utils import _MODELS
from tqdm.auto import tqdm as _tqdm_auto

from ..config import Config, models_dir
from ..messages import Transcript

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_WARMUP_SAMPLES = 16000  # ~1 s of silence @ 16 kHz (DESIGN §7.4 warmup)

_DOWNLOAD_ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

# Model fallback ladder (DESIGN §9): (rung, model selector, device, compute_type).
_FALLBACK_RUNGS: tuple[tuple[int, str, str, str], ...] = (
    (1, "configured", "cuda", "int8_float16"),
    (2, "configured", "cpu", "int8"),
    (3, "small", "cuda", "int8_float16"),
    (4, "small", "cpu", "int8"),
)


def _to_float32(pcm: np.ndarray) -> np.ndarray:
    """faster-whisper expects float32 in [-1, 1]; messages.py's AudioChunk.pcm is int16."""
    if pcm.dtype == np.float32:
        return pcm
    return pcm.astype(np.float32) / 32768.0


def _is_cuda_oom(exc: Exception) -> bool:
    return "out of memory" in str(exc).lower()


_DENOISE_PROP_DECREASE = 0.3  # see _denoise's docstring -- full strength (1.0) gutted real speech


def _denoise(audio: np.ndarray) -> np.ndarray:
    """Spectral-gating background-noise suppression (DESIGN §7.4, user request:
    background noise hurt accuracy noticeably more than it does for Windows'
    own dictation). `stationary=False` adapts to fluctuating noise (traffic,
    background chatter) rather than assuming a fixed noise floor.

    `prop_decrease` is deliberately well below the library default of 1.0
    (full-strength gating): a VAD-trimmed utterance buffer is almost all
    speech with no long quiet stretch to calibrate against, so full-strength
    gating over-fires and guts the speech itself -- confirmed live (default
    1.0 made dictation produce nothing at all, even shouting) and reproduced
    in a benchmark: on a realistic continuous-speech-shaped buffer, 1.0 cut
    RMS energy to ~29% of the original, 0.3 keeps it at ~78%.
    """
    return nr.reduce_noise(
        y=audio, sr=_SAMPLE_RATE, stationary=False, prop_decrease=_DENOISE_PROP_DECREASE
    ).astype(np.float32)


def _progress_tqdm_class(progress_cb: Callable[[float, str], None], label: str) -> type:
    """Builds a tqdm subclass whose `update()` reports fractional progress via `progress_cb`.

    `huggingface_hub.snapshot_download()` instantiates this class itself (as
    its internal "bytes transferred" / "bytes reconstructed" aggregate bars
    across all allow-listed files), so the callback can't be passed as a
    constructor kwarg - it's captured in this closure instead. Verified
    empirically (manual GPU smoke test, see final report) to yield
    reasonably fine-grained, monotonically increasing byte-level progress,
    not just one update per completed file.
    """

    class _ProgressTqdm(_tqdm_auto):
        def update(self, n: float = 1) -> bool | None:
            result = super().update(n)
            if self.total:
                progress_cb(min(self.n / self.total, 1.0), label)
            return result

    return _ProgressTqdm


def _ensure_downloaded(model_name: str, progress_cb: Callable[[float, str], None]) -> Path:
    local_dir = models_dir() / model_name
    if (local_dir / "model.bin").exists():
        progress_cb(1.0, f"{model_name} (cached)")
        return local_dir
    local_dir.mkdir(parents=True, exist_ok=True)
    repo_id = _MODELS.get(model_name, model_name)
    huggingface_hub.snapshot_download(
        repo_id=repo_id,
        local_dir=local_dir,
        allow_patterns=_DOWNLOAD_ALLOW_PATTERNS,
        tqdm_class=_progress_tqdm_class(progress_cb, f"downloading {model_name}"),
    )
    return local_dir


def _warmup(model: WhisperModel) -> None:
    silence = np.zeros(_WARMUP_SAMPLES, dtype=np.float32)
    segments, _info = model.transcribe(
        silence,
        language="en",
        beam_size=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=False,
        without_timestamps=True,
    )
    list(segments)  # the segment generator is lazy; consume it to force the decode


class WhisperLocalBackend:
    """`SttBackend` implementation wrapping `faster_whisper.WhisperModel` (DESIGN §7.4).

    `rung` (1-4) records which entry of the model fallback ladder (§9) is
    currently loaded; `rung > 1` means the caller should show the tray
    DEGRADED state. `descriptor` reflects the active rung, e.g.
    "large-v3-turbo/int8_float16/cuda", or "small/int8/cpu" after a fallback.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._model: WhisperModel | None = None
        self._model_name = ""
        self._device = ""
        self._compute_type = ""
        self.rung = 0

    @property
    def descriptor(self) -> str:
        return f"{self._model_name}/{self._compute_type}/{self._device}"

    def load(self, progress_cb: Callable[[float, str], None]) -> None:
        self._load_from(0, progress_cb)

    def unload(self) -> None:
        self._model = None
        self._model_name = ""
        self._device = ""
        self._compute_type = ""
        self.rung = 0

    def transcribe(
        self,
        pcm: np.ndarray,
        language: str | None,
        hotwords: str | None = None,
        initial_prompt: str | None = None,
    ) -> Transcript:
        if self._model is None:
            raise RuntimeError("WhisperLocalBackend.transcribe() called before load()")
        try:
            return self._decode(pcm, language, hotwords, initial_prompt)
        except Exception as exc:
            if self._device != "cuda" or not _is_cuda_oom(exc):
                raise
            logger.warning(
                "CUDA OOM during transcription at rung %d; dropping a rung and retrying: %s",
                self.rung,
                exc,
            )
            self._drop_rung()
            return self._decode(pcm, language, hotwords, initial_prompt)

    def detect_language_probs(self, pcm: np.ndarray) -> dict[str, float]:
        """Runs faster-whisper's language detection; feeds `language.resolve_language()` (§7.10)."""
        if self._model is None:
            raise RuntimeError("WhisperLocalBackend.detect_language_probs() called before load()")
        audio = _to_float32(pcm)
        _language, _probability, all_probs = self._model.detect_language(audio)
        return dict(all_probs)

    def _decode(
        self,
        pcm: np.ndarray,
        language: str | None,
        hotwords: str | None,
        initial_prompt: str | None,
    ) -> Transcript:
        assert self._model is not None
        audio = _to_float32(pcm)
        if self._config.stt.denoise:
            audio = _denoise(audio)
        combined_prompt = (
            " ".join(p for p in (self._config.stt.initial_prompt, initial_prompt) if p) or None
        )
        segments, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=self._config.stt.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
            hotwords=hotwords,
            initial_prompt=combined_prompt,
        )
        segment_list = list(segments)
        text = "".join(segment.text for segment in segment_list).strip()
        if segment_list:
            avg_logprob = sum(s.avg_logprob for s in segment_list) / len(segment_list)
            no_speech_prob = sum(s.no_speech_prob for s in segment_list) / len(segment_list)
        else:
            avg_logprob = 0.0
            no_speech_prob = 1.0
        return Transcript(
            text=text,
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            duration_s=info.duration,
            language=info.language,
        )

    def _drop_rung(self) -> None:
        index = next(i for i, r in enumerate(_FALLBACK_RUNGS) if r[0] == self.rung)
        self._load_from(index + 1, lambda _frac, _label: None)

    def _load_from(self, start_index: int, progress_cb: Callable[[float, str], None]) -> None:
        last_exc: Exception | None = None
        for rung, which, device, compute_type in _FALLBACK_RUNGS[start_index:]:
            if self._config.stt.device == "cpu" and device == "cuda":
                continue
            if self._config.stt.device == "cuda" and device == "cpu":
                continue
            model_name = self._config.stt.model if which == "configured" else "small"
            try:
                local_dir = _ensure_downloaded(model_name, progress_cb)
                model = WhisperModel(str(local_dir), device=device, compute_type=compute_type)
                _warmup(model)
            except Exception as exc:
                logger.warning(
                    "STT rung %d (%s/%s/%s) failed to load: %s",
                    rung,
                    model_name,
                    compute_type,
                    device,
                    exc,
                )
                last_exc = exc
                continue
            self._model = model
            self.rung = rung
            self._model_name = model_name
            self._device = device
            self._compute_type = compute_type
            progress_cb(1.0, "ready")
            return
        raise RuntimeError(
            f"all STT model fallback rungs failed to load; last error: {last_exc!r}"
        ) from last_exc
