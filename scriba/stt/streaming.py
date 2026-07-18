"""Re-decode loop + LocalAgreement-2 stability policy for streaming partials (DESIGN.md §7.4a).

Delivery model: `StreamingSession` does not own a queue or a thread. The
caller supplies an `emit` sink at construction time - either a plain
`Callable[[Transcript], None]` or any object exposing `.put()` (e.g.
`queue.Queue`) - and every `Transcript` (partial or final) is handed to that
sink synchronously from inside `feed()`. `StreamingSession` never touches
`text/pipeline.py` or injection; a later integration layer owns the `stt`
thread, constructs one `StreamingSession` per open utterance, calls
`.feed()` for every incoming `AudioChunk` for that utterance's `utterance_id`,
and reads `Transcript`s from wherever `emit` delivered them.

`interval_ms`-paced re-decoding is gated on `AudioChunk.t_monotonic` (the
caller's clock), not a real timer/sleep, so this class makes no threads, does
no I/O, and is deterministic to unit test.
"""

import logging
import queue
import time
from collections.abc import Callable, Sequence

import numpy as np

from ..config import Config
from ..messages import AudioChunk, Transcript
from .base import SttBackend

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


def local_agreement_prefix(
    previous_words: Sequence[str], current_words: Sequence[str]
) -> list[str]:
    """LocalAgreement-2 (§7.4a): the longest common prefix of two consecutive decode passes.

    Tokens (here: whitespace-split words) become "committed" once two
    consecutive passes agree on them - this is exactly that prefix, computed
    fresh from the immediately preceding pass and the current one.
    """
    committed: list[str] = []
    for prev_word, curr_word in zip(previous_words, current_words, strict=False):
        if prev_word != curr_word:
            break
        committed.append(prev_word)
    return committed


def partial_text(policy: str, committed_words: Sequence[str], current_words: Sequence[str]) -> str:
    """Applies the `streaming.policy` config (§7.4a) to decide what a partial should show.

    `eager` (default): the latest pass in full, committed or not - later
    passes revise it. `stable`: only the agreed (committed) prefix.
    """
    if policy == "eager":
        return " ".join(current_words)
    if policy == "stable":
        return " ".join(committed_words)
    raise ValueError(f"unknown streaming policy: {policy!r}")


class StreamingSession:
    """Feeds `AudioChunk`s for one `utterance_id` through re-decode + LocalAgreement-2 (§7.4a).

    If `config.streaming.enabled` is False, `feed()` performs exactly one
    decode, on the `is_final` chunk - the same output shape (a single
    `Transcript(is_partial=False, ...)`) as the streaming path's final pass.

    Window management (§7.4a): once the accumulated buffer exceeds
    `streaming.window_s`, the oldest audio is dropped and the text already
    committed from the last completed pass is kept as `initial_prompt`
    context instead, so re-decode cost stays bounded on long utterances.
    Because `without_timestamps=True` (no word-level audio alignment is
    available), this is an approximation: it assumes the most recent
    completed pass's words correspond to the audio being dropped, which is
    accurate once a few passes have run (the common case, since
    `window_s` default 15s >> `interval_ms` default 800ms) but can lose a
    sliver of leading audio if a window overflow happens before any decode
    pass has completed.
    """

    def __init__(
        self,
        backend: SttBackend,
        config: Config,
        emit: "Callable[[Transcript], None] | queue.Queue",
        hotwords: str | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._emit: Callable[[Transcript], None] = (
            emit.put if isinstance(emit, queue.Queue) else emit
        )
        self._hotwords = hotwords
        self._utterance_id: int | None = None
        self._language: str | None = None
        self._buffer = np.zeros(0, dtype=np.int16)
        self._prefix_text = ""
        self._prev_pass_words: list[str] = []
        self._last_decode_t = 0.0

    def feed(self, chunk: AudioChunk) -> None:
        if self._utterance_id is None:
            self._utterance_id = chunk.utterance_id
            self._language = chunk.language
            self._last_decode_t = chunk.t_monotonic
        elif chunk.utterance_id != self._utterance_id:
            raise ValueError(
                f"StreamingSession is bound to utterance_id={self._utterance_id}, "
                f"got a chunk for utterance_id={chunk.utterance_id}"
            )

        if chunk.pcm is not None and chunk.pcm.size:
            self._buffer = np.concatenate([self._buffer, chunk.pcm])

        if chunk.is_final:
            self._decode(is_final=True)
            self._reset()
            return

        if not self._config.streaming.enabled:
            return

        self._enforce_window()

        interval_s = self._config.streaming.interval_ms / 1000.0
        if chunk.t_monotonic - self._last_decode_t >= interval_s:
            self._last_decode_t = chunk.t_monotonic
            self._decode(is_final=False)

    def _reset(self) -> None:
        self._utterance_id = None
        self._language = None
        self._buffer = np.zeros(0, dtype=np.int16)
        self._prefix_text = ""
        self._prev_pass_words = []
        self._last_decode_t = 0.0

    def _enforce_window(self) -> None:
        window_samples = self._config.streaming.window_s * _SAMPLE_RATE
        if self._buffer.size <= window_samples:
            return
        drop = self._buffer.size - window_samples
        if self._prev_pass_words:
            self._prefix_text = " ".join([self._prefix_text, *self._prev_pass_words]).strip()
        self._buffer = self._buffer[drop:]
        self._prev_pass_words = []

    def _decode(self, is_final: bool) -> None:
        assert self._utterance_id is not None
        t0 = time.perf_counter()
        transcript = self._backend.transcribe(
            self._buffer,
            self._language,
            hotwords=self._hotwords,
            initial_prompt=self._prefix_text or None,
        )
        wall_ms = (time.perf_counter() - t0) * 1000
        transcript.utterance_id = self._utterance_id
        log = logger.info if is_final else logger.debug
        log(
            "utterance %d %s decode: %.0f ms audio in %.0f ms wall -> %r",
            self._utterance_id,
            "final" if is_final else "partial",
            self._buffer.size / _SAMPLE_RATE * 1000,
            wall_ms,
            transcript.text,
        )

        pass_text = (
            " ".join([self._prefix_text, transcript.text]).strip()
            if self._prefix_text
            else transcript.text
        )
        current_words = pass_text.split()

        if is_final:
            transcript.text = pass_text
            transcript.is_partial = False
            self._emit(transcript)
            return

        committed = local_agreement_prefix(self._prev_pass_words, current_words)
        transcript.text = partial_text(self._config.streaming.policy, committed, current_words)
        transcript.is_partial = True
        self._prev_pass_words = current_words
        self._emit(transcript)
