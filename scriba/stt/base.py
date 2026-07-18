"""`SttBackend` protocol - the seam for future backends (DESIGN.md §7.4).

Deviation from the DESIGN.md §7.4 snippet: that snippet's
`transcribe(utt: Utterance, hotwords: str | None)` used the design doc's
original single-shot `Utterance` message, which was replaced by `AudioChunk`
(see messages.py's module docstring). By the time a decode pass actually
runs, `streaming.py` has already assembled a contiguous audio buffer out of
one or more `AudioChunk`s, so `transcribe()` here takes a raw int16 PCM
buffer plus the resolved language directly, instead of a message object.
`hotwords` (vocabulary biasing, M2 - unused for now) and `initial_prompt`
(decoder priming; also used by `streaming.py`'s window management, §7.4a, to
carry already-committed text forward once older audio is dropped from the
re-decode window) are both optional.
"""

from collections.abc import Callable
from typing import Protocol

import numpy as np

from ..messages import Transcript


class SttBackend(Protocol):
    def load(self, progress_cb: Callable[[float, str], None]) -> None: ...

    def transcribe(
        self,
        pcm: np.ndarray,
        language: str | None,
        hotwords: str | None = None,
        initial_prompt: str | None = None,
    ) -> Transcript: ...

    def unload(self) -> None: ...

    @property
    def descriptor(self) -> str: ...  # e.g. "large-v3-turbo/int8_float16/cuda"
