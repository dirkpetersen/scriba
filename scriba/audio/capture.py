"""Microphone capture: per-device streams, ring buffers, hot-plug (DESIGN.md §7.1).

Public surface: `AudioCapture` (owns the open streams + pre-roll buffers) and
the module-level `list_devices()` (pure enumeration, usable without an
`AudioCapture` instance for the settings UI / `--diagnose`).
"""

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from ..config import Config
from ..messages import AudioFrame, device_id_for_name

logger = logging.getLogger(__name__)

_TARGET_RATE = 16000
_BLOCKSIZE = 512
_POLL_INTERVAL_S = 3.0


@dataclass
class DeviceInfo:
    id: str
    name: str
    is_default: bool
    enabled: bool


class RingBuffer:
    """Fixed-capacity rolling buffer of the most recent int16 mono samples."""

    def __init__(self, capacity: int):
        self._capacity = max(0, capacity)
        self._buf = np.zeros(self._capacity, dtype=np.int16)
        self._pos = 0
        self._fill = 0
        self._lock = threading.Lock()

    def push(self, samples: np.ndarray) -> None:
        if self._capacity == 0 or len(samples) == 0:
            return
        with self._lock:
            n = len(samples)
            if n >= self._capacity:
                self._buf[:] = samples[-self._capacity :]
                self._pos = 0
                self._fill = self._capacity
                return
            end = self._pos + n
            if end <= self._capacity:
                self._buf[self._pos : end] = samples
            else:
                first = self._capacity - self._pos
                self._buf[self._pos :] = samples[:first]
                self._buf[: n - first] = samples[first:]
            self._pos = end % self._capacity
            self._fill = min(self._capacity, self._fill + n)

    def read(self) -> np.ndarray:
        with self._lock:
            if self._fill < self._capacity:
                return self._buf[: self._fill].copy()
            return np.concatenate([self._buf[self._pos :], self._buf[: self._pos]])


def resample_to_16k(pcm: np.ndarray, source_rate: int) -> np.ndarray:
    """Resample int16 mono `pcm` from `source_rate` to 16 kHz int16 mono."""
    if source_rate == _TARGET_RATE:
        return pcm.astype(np.int16, copy=False)
    gcd = math.gcd(source_rate, _TARGET_RATE)
    up, down = _TARGET_RATE // gcd, source_rate // gcd
    resampled = resample_poly(pcm.astype(np.float32), up, down)
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)


_device_id = device_id_for_name


class FrameChunker:
    """Re-chunks a variable-length sample stream into exact 512-sample frames.

    The native-rate resample path can produce off-by-one frame lengths (e.g. a
    22050 Hz device's 706-sample block resamples to 513 samples), and Silero
    VAD hard-requires 512 -- so resampled output is accumulated here and only
    complete frames are emitted; the remainder carries into the next callback.
    """

    def __init__(self, frame_samples: int = _BLOCKSIZE):
        self._frame_samples = frame_samples
        self._residual = np.zeros(0, dtype=np.int16)

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        buf = np.concatenate([self._residual, samples])
        n_frames = len(buf) // self._frame_samples
        self._residual = buf[n_frames * self._frame_samples :]
        return [
            buf[i * self._frame_samples : (i + 1) * self._frame_samples]
            for i in range(n_frames)
        ]


def _best_input_entries() -> dict[str, dict]:
    """Enumerate input-capable devices, deduped by name (preferring WASAPI on Windows).

    The same physical microphone shows up once per host API (MME, DirectSound,
    WASAPI, WDM-KS); DESIGN.md §7.1 names WASAPI as the intended host API, and
    picking a single entry per name avoids opening the same mic multiple times
    and keeps `device_id` (derived from the name) unambiguous.
    """
    hostapis = sd.query_hostapis()
    best: dict[str, dict] = {}
    for entry in sd.query_devices():
        if entry["max_input_channels"] <= 0:
            continue
        name = entry["name"].strip()
        if not name:
            continue
        is_wasapi = hostapis[entry["hostapi"]]["name"] == "Windows WASAPI"
        current = best.get(name)
        if current is None or (is_wasapi and not current["_is_wasapi"]):
            best[name] = {**entry, "name": name, "_is_wasapi": is_wasapi}
    return best


def _default_input_name() -> str | None:
    try:
        idx = sd.default.device[0]
        if idx is None or idx < 0:
            return None
        info = sd.query_devices(idx)
        if info["max_input_channels"] <= 0:
            return None
        return info["name"].strip()
    except Exception:
        return None


def list_devices(enabled_devices: list[str] | None = None) -> list[DeviceInfo]:
    """Enumerate input devices. Empty/None `enabled_devices` means all are enabled."""
    enabled_devices = enabled_devices or []
    default_name = _default_input_name()
    entries = _best_input_entries()
    return [
        DeviceInfo(
            id=_device_id(name),
            name=name,
            is_default=(name == default_name),
            enabled=(not enabled_devices or name in enabled_devices),
        )
        for name in sorted(entries)
    ]


class AudioCapture:
    """Owns one PortAudio InputStream per enabled device, plus hot-plug polling.

    `frame_queue` receives `AudioFrame`s pushed from PortAudio callback threads.
    """

    def __init__(
        self,
        config: Config,
        frame_queue: "queue.Queue[AudioFrame]",
        poll_interval_s: float = _POLL_INTERVAL_S,
    ):
        self._config = config
        self._frame_queue = frame_queue
        self._poll_interval_s = poll_interval_s
        self._streams: dict[str, sd.InputStream] = {}
        self._prerolls: dict[str, RingBuffer] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self._open_matching_devices()
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=self._poll_interval_s + 1)
            self._poll_thread = None
        with self._lock:
            for device_id in list(self._streams):
                self._close_device(device_id)

    def get_preroll(self, device_id: str) -> np.ndarray:
        with self._lock:
            buf = self._prerolls.get(device_id)
        if buf is None:
            return np.zeros(0, dtype=np.int16)
        return buf.read()

    def list_devices(self) -> list[DeviceInfo]:
        return list_devices(self._config.audio.enabled_devices)

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval_s):
            try:
                self._open_matching_devices()
                self._drop_missing_devices()
            except Exception:
                logger.exception("device hot-plug poll failed")

    def _open_matching_devices(self) -> None:
        entries = _best_input_entries()
        enabled_devices = self._config.audio.enabled_devices
        with self._lock:
            for name, entry in entries.items():
                device_id = _device_id(name)
                enabled = not enabled_devices or name in enabled_devices
                if enabled and device_id not in self._streams:
                    self._open_device(device_id, entry)

    def _drop_missing_devices(self) -> None:
        present_ids = {_device_id(name) for name in _best_input_entries()}
        with self._lock:
            for device_id in list(self._streams):
                if device_id not in present_ids:
                    logger.warning("input device %s disappeared, closing stream", device_id)
                    self._close_device(device_id)

    def _open_device(self, device_id: str, entry: dict) -> None:
        name = entry["name"]
        index = entry["index"]
        native_rate = int(entry["default_samplerate"])
        pre_roll_samples = round(self._config.vad.pre_roll_ms * _TARGET_RATE / 1000)
        preroll = RingBuffer(pre_roll_samples)

        stream = self._try_open(index, _TARGET_RATE, _BLOCKSIZE, device_id, preroll)
        if stream is None:
            native_blocksize = max(1, round(native_rate * _BLOCKSIZE / _TARGET_RATE))
            stream = self._try_open(index, native_rate, native_blocksize, device_id, preroll)
        if stream is None:
            logger.error("failed to open input device %r (id=%s)", name, device_id)
            return

        self._streams[device_id] = stream
        self._prerolls[device_id] = preroll
        logger.info("opened input device %r (id=%s)", name, device_id)

    def _try_open(
        self, index: int, samplerate: int, blocksize: int, device_id: str, preroll: RingBuffer
    ) -> sd.InputStream | None:
        stream = None
        try:
            stream = sd.InputStream(
                device=index,
                channels=1,
                samplerate=samplerate,
                dtype="int16",
                blocksize=blocksize,
                callback=self._make_callback(device_id, preroll, samplerate),
            )
            stream.start()
            return stream
        except sd.PortAudioError:
            logger.debug(
                "open at %d Hz failed for device %s", samplerate, device_id, exc_info=True
            )
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            return None

    def _close_device(self, device_id: str) -> None:
        stream = self._streams.pop(device_id, None)
        self._prerolls.pop(device_id, None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                logger.exception("error closing stream for device %s", device_id)

    def _make_callback(self, device_id: str, preroll: RingBuffer, source_rate: int):
        chunker = FrameChunker() if source_rate != _TARGET_RATE else None

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning("stream status for device %s: %s", device_id, status)
            pcm = indata[:, 0]
            if chunker is not None:
                out_frames = chunker.push(resample_to_16k(pcm, source_rate))
            else:
                out_frames = [pcm.copy()]
            for frame in out_frames:
                preroll.push(frame)
                try:
                    self._frame_queue.put_nowait(AudioFrame(device_id, frame, time.monotonic()))
                except queue.Full:
                    pass

        return callback
