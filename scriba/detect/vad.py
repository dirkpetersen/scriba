"""Silero VAD ONNX wrapper and the utterance segmentation state machine (DESIGN.md §7.2).

Deviation from the `silero-vad` PyPI package (see CLAUDE.md / DESIGN.md §8):
that package unconditionally pulls in torch+torchaudio (multi-GB), which
contradicts this project's "tiny ONNX, no torch" choice for VAD. Instead this
module depends only on `onnxruntime` and downloads the ~2 MB `silero_vad.onnx`
file straight from the snakers4/silero-vad GitHub repo (tag v6.2.1) into
`scriba.config.models_dir()`, reusing it on subsequent runs.

The ONNX calling convention below (input names `input`/`state`/`sr`, the
64-sample recurrent "context" concatenated before each 512-sample frame, the
(2, 1, 128) state tensor) was confirmed against `src/silero_vad/utils_vad.py`
(`OnnxWrapper`) at that same tag -- it is not part of any public API contract,
so if a future model tag changes it, this wrapper needs updating too.
"""

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
import requests

from ..config import Config, VadConfig, models_dir
from ..messages import AudioChunk, AudioFrame
from .arbiter import MicArbiter

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512
_FRAME_MS = _FRAME_SAMPLES / _SAMPLE_RATE * 1000  # 32.0 ms, fixed by DESIGN §7.1's blocksize=512
_CONTEXT_SAMPLES = 64
_STATE_SHAPE = (2, 1, 128)
_INT16_FULL_SCALE = 32768.0

_MODEL_TAG = "v6.2.1"
_MODEL_URL = (
    f"https://raw.githubusercontent.com/snakers4/silero-vad/{_MODEL_TAG}"
    "/src/silero_vad/data/silero_vad.onnx"
)
_MODEL_FILENAME = "silero_vad.onnx"


def ensure_model_downloaded(path: Path | None = None) -> Path:
    """Downloads `silero_vad.onnx` to `path` if it isn't already there; returns `path`.

    Default `path` is `models_dir() / "silero_vad.onnx"` (same provisioning
    treatment as the STT model, DESIGN.md §7.4).
    """
    path = path or models_dir() / _MODEL_FILENAME
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading Silero VAD model from %s", _MODEL_URL)
    response = requests.get(_MODEL_URL, timeout=30)
    response.raise_for_status()
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_bytes(response.content)
    tmp_path.replace(path)
    return path


class SileroVad:
    """Stateful per-stream wrapper around the Silero VAD ONNX model.

    One instance owns exactly one recurrent state; use one instance per
    monitored device (DESIGN §7.2: "Run Silero VAD independently per enabled
    device") so devices' VAD runs never interfere with each other.
    """

    def __init__(self, model_path: Path | None = None):
        self._model_path = ensure_model_downloaded(model_path)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(self._model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._state: np.ndarray = np.zeros(0)
        self._context: np.ndarray = np.zeros(0)
        self.reset()

    def reset(self) -> None:
        """Clears recurrent state; call when this device's stream restarts."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT_SAMPLES), dtype=np.float32)

    def process_frame(self, pcm: np.ndarray) -> float:
        """Returns the speech probability for one 512-sample @16kHz int16 mono frame."""
        if pcm.shape[-1] != _FRAME_SAMPLES:
            raise ValueError(f"expected {_FRAME_SAMPLES} samples, got {pcm.shape[-1]}")
        chunk = pcm.astype(np.float32).reshape(1, _FRAME_SAMPLES) / _INT16_FULL_SCALE
        x = np.concatenate([self._context, chunk], axis=1)
        sr = np.array(_SAMPLE_RATE, dtype=np.int64)
        out, state = self._session.run(None, {"input": x, "state": self._state, "sr": sr})
        self._state = state
        self._context = x[:, -_CONTEXT_SAMPLES:]
        return float(out.item())


@dataclass
class _PendingFrame:
    pcm: np.ndarray
    t_monotonic: float


class UtteranceSegmenter:
    """Speech segmentation state machine (DESIGN.md §7.2), fed one already
    VAD-scored frame at a time.

    Deliberately clock- and model-free: probabilities and timestamps are both
    supplied by the caller (the detector orchestrator, using the winning
    device's frames per the MicArbiter's choice), so this class is fully
    testable with synthetic probability sequences -- see
    tests/test_vad_segmentation.py.

    Only one utterance is ever open at a time (this mirrors the arbiter: at
    most one device feeds an utterance system-wide), so a single instance is
    shared across whichever device currently holds the arbiter's win.

    Guard semantics (see docstrings on `process_frame` internals for the
    precise per-frame algorithm):
    - "Speech start" fires on 2 consecutive frames >= threshold; the pre-roll
      buffer is captured *at that moment* (not later) so it can never overlap
      with frames buffered after the trigger.
    - The `min_speech_ms` guard is evaluated against the speech-only span
      (frames from trigger to the *last* frame still >= threshold, ignoring
      any trailing not-yet-endpointed silence) -- not wall-clock time since
      trigger -- so a brief blip (e.g. a cough) followed by silence is
      silently discarded (no chunk emitted at all, ever) rather than merely
      delayed. Until this guard passes, frames are buffered but nothing is
      emitted; once it passes, the buffered audio (pre-roll + everything
      since trigger) is flushed as the first incremental chunk, and every
      subsequent frame is emitted immediately (this is the "incremental while
      the utterance is open" behavior DESIGN.md asks for).
    - `max_utterance_s` force-flushes a long-running confirmed utterance: once
      the limit is reached, we cut at the next frame that dips below
      threshold (a natural pause) rather than mid-word; if speech never dips,
      a hard cap of one more `endpoint_silence_ms` worth of frames forces the
      cut anyway so utterance length stays bounded. The cut chunk carries
      `is_final=True`, and a new utterance (fresh `utterance_id`, no pre-roll,
      since it's a seamless continuation) begins on the very next frame.
    """

    def __init__(
        self,
        vad_config: VadConfig,
        get_preroll: Callable[[str], np.ndarray | None],
    ):
        self._threshold = vad_config.threshold
        self._min_speech_frames = round(vad_config.min_speech_ms / _FRAME_MS)
        self._endpoint_frames = max(1, round(vad_config.endpoint_silence_ms / _FRAME_MS))
        self._max_frames = max(1, round(vad_config.max_utterance_s * 1000 / _FRAME_MS))
        self._get_preroll = get_preroll
        self._next_utterance_id = 1
        self._reset_idle()

    @property
    def is_idle(self) -> bool:
        """True once no candidate/confirmed utterance is in flight.

        The detector orchestrator uses this to know when it's safe to let the
        MicArbiter re-arbitrate for the next utterance (DESIGN §7.2: the
        winner is sticky "for the whole utterance", not forever).
        """
        return not self._triggered and self._pretrig_frame is None

    def reset(self) -> None:
        """Discards any in-flight candidate/confirmed utterance without emitting anything."""
        self._reset_idle()

    def _reset_idle(self) -> None:
        self._triggered = False
        self._confirmed = False
        self._pretrig_frame: _PendingFrame | None = None
        self._pending: list[_PendingFrame] = []
        self._preroll: np.ndarray | None = None
        self._utterance_id: int | None = None
        self._frames_since_start = 0
        self._speech_span_frames = 0
        self._silence_run = 0

    def _mint_utterance_id(self) -> int:
        utterance_id = self._next_utterance_id
        self._next_utterance_id += 1
        return utterance_id

    def process_frame(
        self, device_id: str, pcm: np.ndarray, prob: float, t_monotonic: float
    ) -> AudioChunk | None:
        """Feed one frame; returns an `AudioChunk` if this frame produced one, else None."""
        if not self._triggered:
            return self._process_pretrigger(device_id, pcm, prob, t_monotonic)
        return self._process_triggered(device_id, pcm, prob, t_monotonic)

    def _process_pretrigger(
        self, device_id: str, pcm: np.ndarray, prob: float, t_monotonic: float
    ) -> AudioChunk | None:
        if prob < self._threshold:
            self._pretrig_frame = None
            return None
        if self._pretrig_frame is None:
            self._pretrig_frame = _PendingFrame(pcm, t_monotonic)
            return None

        # Second consecutive qualifying frame: trigger. Capture pre-roll now,
        # before any further audio is buffered, so it can't double-count.
        self._triggered = True
        self._confirmed = False
        self._frames_since_start = 2
        self._speech_span_frames = 2
        self._silence_run = 0
        self._pending = [self._pretrig_frame, _PendingFrame(pcm, t_monotonic)]
        self._preroll = self._get_preroll(device_id)
        self._pretrig_frame = None

        if self._speech_span_frames >= self._min_speech_frames:
            return self._confirm(device_id, t_monotonic)
        return None

    def _process_triggered(
        self, device_id: str, pcm: np.ndarray, prob: float, t_monotonic: float
    ) -> AudioChunk | None:
        self._frames_since_start += 1
        if prob >= self._threshold:
            self._silence_run = 0
            self._speech_span_frames = self._frames_since_start
        else:
            self._silence_run += 1

        if self._silence_run >= self._endpoint_frames:
            was_confirmed = self._confirmed
            utterance_id = self._utterance_id
            self._reset_idle()
            if was_confirmed:
                return AudioChunk(
                    utterance_id=utterance_id,  # type: ignore[arg-type]
                    device_id=device_id,
                    pcm=pcm,
                    t_monotonic=t_monotonic,
                    is_final=True,
                )
            return None  # discarded candidate: shorter than min_speech_ms, emit nothing

        if not self._confirmed:
            self._pending.append(_PendingFrame(pcm, t_monotonic))
            if self._speech_span_frames >= self._min_speech_frames:
                return self._confirm(device_id, t_monotonic)
            return None

        if self._frames_since_start >= self._max_frames:
            hard_cap_reached = self._frames_since_start >= self._max_frames + self._endpoint_frames
            if prob < self._threshold or hard_cap_reached:
                chunk = AudioChunk(
                    utterance_id=self._utterance_id,  # type: ignore[arg-type]
                    device_id=device_id,
                    pcm=pcm,
                    t_monotonic=t_monotonic,
                    is_final=True,
                )
                self._start_continuation(device_id)
                return chunk

        return AudioChunk(
            utterance_id=self._utterance_id,  # type: ignore[arg-type]
            device_id=device_id,
            pcm=pcm,
            t_monotonic=t_monotonic,
            is_final=False,
        )

    def _confirm(self, device_id: str, t_monotonic: float) -> AudioChunk:
        utterance_id = self._mint_utterance_id()
        preroll = self._preroll if self._preroll is not None else np.zeros(0, dtype=np.int16)
        pcm = np.concatenate([preroll, *(f.pcm for f in self._pending)])
        self._utterance_id = utterance_id
        self._confirmed = True
        self._pending = []
        self._preroll = None
        return AudioChunk(
            utterance_id=utterance_id,
            device_id=device_id,
            pcm=pcm,
            t_monotonic=t_monotonic,
            is_final=False,
        )

    def _start_continuation(self, device_id: str) -> None:
        del device_id  # same device by construction (arbiter stays sticky across the cut)
        self._utterance_id = self._mint_utterance_id()
        self._frames_since_start = 0
        self._speech_span_frames = 0
        self._silence_run = 0
        self._confirmed = True
        self._triggered = True
        self._pending = []
        self._preroll = None


class Detector:
    """The `detector` thread (DESIGN.md §5): mic arbiter + VAD + segmentation.

    Consumes `AudioFrame` from `frame_queue` (one PortAudio callback thread
    feeds each enabled device, per `scriba/audio/capture.py`), runs each
    device's own `SileroVad` independently, feeds the `MicArbiter`, drives a
    single shared `UtteranceSegmenter` on whichever device the arbiter has
    picked as the winner, and pushes resulting `AudioChunk`s onto
    `chunk_queue` for the STT worker.

    `SileroVad` instances are created lazily per `device_id` the first time a
    frame from that device is seen, so this class needs no static device
    list and adapts automatically to hot-plugged devices (DESIGN §7.1).
    """

    def __init__(
        self,
        config: Config,
        frame_queue: "queue.Queue[AudioFrame]",
        chunk_queue: "queue.Queue[AudioChunk]",
        get_preroll: Callable[[str], np.ndarray | None],
    ):
        self._config = config
        self._frame_queue = frame_queue
        self._chunk_queue = chunk_queue
        self._vads: dict[str, SileroVad] = {}
        self._arbiter = MicArbiter(config.audio)
        self._segmenter = UtteranceSegmenter(config.vad, get_preroll)

    def run(self, stop_event: threading.Event) -> None:
        """Blocking loop: drains `frame_queue` until `stop_event` is set."""
        while not stop_event.is_set():
            try:
                frame = self._frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._handle_frame(frame)
            except Exception:
                logger.exception("detector: error handling frame from %s", frame.device_id)

    def _handle_frame(self, frame: AudioFrame) -> None:
        # Not dict.setdefault(id, SileroVad()): its default arg is evaluated
        # eagerly, which would rebuild the ~190 ms ONNX session on EVERY
        # frame (frames arrive every ~16 ms with two mics) -- the detector
        # falls 12x behind and dictation appears dead.
        vad = self._vads.get(frame.device_id)
        if vad is None:
            vad = self._vads[frame.device_id] = SileroVad()
        prob = vad.process_frame(frame.pcm)
        rms = float(np.sqrt(np.mean(frame.pcm.astype(np.float64) ** 2)))

        winner = self._arbiter.offer(
            frame.device_id, prob, rms, frame.t_monotonic, self._config.vad.threshold
        )
        if winner != frame.device_id:
            return

        chunk = self._segmenter.process_frame(frame.device_id, frame.pcm, prob, frame.t_monotonic)
        if chunk is not None:
            self._chunk_queue.put(chunk)
        if self._segmenter.is_idle:
            self._arbiter.reset()
