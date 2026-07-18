"""Dataclasses passed between pipeline stages over the thread queues (DESIGN.md §5).

Deviation from DESIGN.md §5 (note also added to DESIGN.md): the single
end-of-utterance `Utterance` message is replaced by `AudioChunk`, emitted
incrementally by the detector while an utterance is open. Streaming re-decode
(§7.4a) needs audio before the VAD endpoint fires; non-streaming mode is the
same shape with exactly one `is_final=True` chunk carrying the whole
utterance, so both modes share one message type and one STT-worker code path.

`utterance_id` is a monotonically increasing counter minted by the detector
per utterance; it threads through `AudioChunk` -> `Transcript` -> `InjectJob`
so the injector can tell "new utterance, reset my typed-string tracker" from
"revision of the utterance I'm already typing".
"""

import hashlib
from dataclasses import dataclass

import numpy as np


def device_id_for_name(name: str) -> str:
    """Stable device identifier derived from the device name.

    Lives here (not in audio/capture.py) because both the producer of
    `AudioFrame.device_id` and consumers that must translate configured
    device *names* (e.g. `audio.device_priority`) need it without importing
    each other.
    """
    return hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:12]


@dataclass
class AudioFrame:
    device_id: str
    pcm: np.ndarray  # int16 mono, 512 samples @ 16 kHz (32 ms)
    t_monotonic: float


@dataclass
class AudioChunk:
    utterance_id: int
    device_id: str
    pcm: np.ndarray | None  # incremental int16 mono samples; None on a pure finalize marker
    t_monotonic: float
    is_final: bool = False  # True on the chunk carrying (or following) the VAD endpoint
    language: str | None = None  # language-policy resolution, set on the utterance's first chunk


@dataclass
class Transcript:
    text: str
    avg_logprob: float
    no_speech_prob: float
    duration_s: float
    language: str
    utterance_id: int = 0
    is_partial: bool = False


@dataclass
class InjectJob:
    text: str  # text to type; may contain '\n' (injected as Enter)
    erase: int = 0  # backspaces to send first (streaming revision, §7.4a)
    utterance_id: int = 0
    is_final: bool = False


@dataclass
class ForegroundWindow:
    hwnd: int
    title: str
    exe_name: str


@dataclass
class PostprocState:
    """Carried across utterances by the caller (not mutated in place by pipeline()).

    `capitalize_next`/`space_before_next` drive DESIGN §7.5 point 5. Initial
    state and post-window-change state are identical: capitalize, no leading
    space, as if dictation just started.
    """

    capitalize_next: bool = True
    space_before_next: bool = False
    last_hwnd: int | None = None
