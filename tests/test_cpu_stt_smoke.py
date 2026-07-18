"""Real STT integration smoke test -- CPU only, tiny model.

Unlike test_whisper_local.py (pure helpers, no model loaded), this exercises
`WhisperLocalBackend.load()` -> `.transcribe()` end-to-end against the actual
faster-whisper/CTranslate2 code path, using the `tiny` model on CPU so it's
cheap enough to run without a GPU. Before this test existed, that integration
path had zero CI coverage: it was only ever covered by `@pytest.mark.gpu`
tests, which are excluded by default (DESIGN.md §10) and need real GPU
hardware + the full-size production model.

This is deliberately NOT a `gpu`-marked test and is NOT selected by a plain
`pytest` run either -- see the `cpu_stt` marker registered in
pyproject.toml's `[tool.pytest.ini_options]`, and note that `addopts`
excludes it there too (`-m "not gpu and not cpu_stt"`), because it needs
network access on first run to download the tiny model (~75 MB) from
Hugging Face. CI selects it explicitly with `pytest -m cpu_stt` in the
`cpu-stt-smoke` job (see `.github/workflows/ci.yml`).

The assertions intentionally do not check the transcribed text for
correctness: a synthetic sine-wave "utterance" has no real speech content,
and CPU/tiny-model accuracy on top of that would be meaningless to assert
on. The point is to catch integration regressions (e.g. a broken
CTranslate2/faster-whisper API call, a bad Config wiring) by exercising
load() -> transcribe() and checking the result is a well-formed Transcript.
"""

import numpy as np
import pytest

from scriba.config import Config
from scriba.stt.whisper_local import WhisperLocalBackend

pytestmark = pytest.mark.cpu_stt

_SAMPLE_RATE = 16000


def _synthetic_utterance(duration_s: float = 2.0, sr: int = _SAMPLE_RATE) -> np.ndarray:
    """A synthetic int16 PCM buffer shaped like `AudioChunk.pcm` (see messages.py).

    Not real speech (no TTS dependency available/needed here) -- just a tone
    with some amplitude modulation so it isn't pure silence, which is enough
    to drive the decode path without asserting anything about its accuracy.
    """
    t = np.arange(int(duration_s * sr)) / sr
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 2.0 * t)
    tone = envelope * np.sin(2 * np.pi * 220.0 * t)
    return (tone * 32767 * 0.6).astype(np.int16)


def test_whisper_local_backend_loads_and_transcribes_on_cpu():
    config = Config()
    config.stt.model = "tiny"
    config.stt.device = "cpu"
    config.stt.compute_type = "int8"

    backend = WhisperLocalBackend(config)
    progress_events: list[tuple[float, str]] = []
    try:
        backend.load(lambda frac, label: progress_events.append((frac, label)))

        # rung 2 == (configured model, cpu, int8) per _FALLBACK_RUNGS -- rung 1
        # (cuda) is skipped because config.stt.device == "cpu".
        assert backend.rung == 2
        assert backend.descriptor == "tiny/int8/cpu"
        assert progress_events, "load() should report at least one progress event"

        transcript = backend.transcribe(_synthetic_utterance(), language="en")

        assert isinstance(transcript.text, str)
        assert transcript.duration_s > 0
        assert transcript.language
        assert 0.0 <= transcript.no_speech_prob <= 1.0
        assert isinstance(transcript.avg_logprob, float)
    finally:
        backend.unload()
