import queue

import numpy as np
import pytest

from scriba.config import Config
from scriba.messages import AudioChunk, Transcript
from scriba.stt.streaming import StreamingSession, local_agreement_prefix, partial_text


class _FakeBackend:
    """Test double for SttBackend: returns canned, progressively-extending text per call."""

    def __init__(self, texts: list[str]):
        self.texts = texts
        self.calls: list[dict] = []

    def load(self, progress_cb):
        pass

    def unload(self):
        pass

    @property
    def descriptor(self) -> str:
        return "fake/int8/cpu"

    def transcribe(self, pcm, language, hotwords=None, initial_prompt=None):
        index = min(len(self.calls), len(self.texts) - 1)
        self.calls.append(
            {
                "pcm_len": int(pcm.size),
                "language": language,
                "hotwords": hotwords,
                "initial_prompt": initial_prompt,
            }
        )
        return Transcript(
            text=self.texts[index],
            avg_logprob=-0.2,
            no_speech_prob=0.05,
            duration_s=pcm.size / 16000,
            language=language or "en",
        )


def _chunk(utterance_id, t, pcm=None, is_final=False, language=None):
    return AudioChunk(
        utterance_id=utterance_id,
        device_id="mic0",
        pcm=pcm,
        t_monotonic=t,
        is_final=is_final,
        language=language,
    )


def _pcm(n_samples=1600):
    return np.zeros(n_samples, dtype=np.int16)


# --- local_agreement_prefix (LocalAgreement-2) ---


def test_local_agreement_prefix_full_match():
    assert local_agreement_prefix(["a", "b", "c"], ["a", "b", "c"]) == ["a", "b", "c"]


def test_local_agreement_prefix_divergence():
    assert local_agreement_prefix(["a", "b", "c"], ["a", "b", "d"]) == ["a", "b"]


def test_local_agreement_prefix_empty_previous():
    assert local_agreement_prefix([], ["a", "b"]) == []


def test_local_agreement_prefix_empty_current():
    assert local_agreement_prefix(["a", "b"], []) == []


def test_local_agreement_prefix_current_shorter():
    assert local_agreement_prefix(["a", "b", "c"], ["a"]) == ["a"]


def test_local_agreement_prefix_immediate_divergence():
    assert local_agreement_prefix(["x"], ["y"]) == []


def test_local_agreement_prefix_current_longer_extends_beyond_previous():
    # zip stops at the shorter list -- extra words on the longer pass aren't compared.
    assert local_agreement_prefix(["a", "b"], ["a", "b", "c", "d"]) == ["a", "b"]


# --- partial_text (eager / stable policy) ---


def test_partial_text_eager_ignores_committed():
    assert partial_text("eager", ["a"], ["a", "b", "c"]) == "a b c"


def test_partial_text_stable_uses_committed_only():
    assert partial_text("stable", ["a", "b"], ["a", "b", "c"]) == "a b"


def test_partial_text_stable_with_nothing_committed_yet():
    assert partial_text("stable", [], ["a", "b", "c"]) == ""


def test_partial_text_unknown_policy_raises():
    with pytest.raises(ValueError):
        partial_text("bogus", [], [])


# --- StreamingSession ---


def test_non_streaming_single_final_decode():
    config = Config()
    config.streaming.enabled = False
    backend = _FakeBackend(["hello world"])
    results: list[Transcript] = []
    session = StreamingSession(backend, config, results.append)

    session.feed(_chunk(1, 0.0, _pcm(), language="en"))
    session.feed(_chunk(1, 0.1, _pcm()))
    session.feed(_chunk(1, 5.0, None, is_final=True))

    assert len(results) == 1
    assert results[0].is_partial is False
    assert results[0].text == "hello world"
    assert results[0].utterance_id == 1
    assert len(backend.calls) == 1


def test_streaming_disabled_ignores_interval_and_never_emits_partials():
    config = Config()
    config.streaming.enabled = False
    config.streaming.interval_ms = 1  # would fire constantly if (incorrectly) honored
    backend = _FakeBackend(["final text"])
    results: list[Transcript] = []
    session = StreamingSession(backend, config, results.append)

    t = 0.0
    session.feed(_chunk(1, t, _pcm(), language="en"))
    for _ in range(10):
        t += 0.5
        session.feed(_chunk(1, t, _pcm()))
    session.feed(_chunk(1, t + 0.1, None, is_final=True))

    assert all(not r.is_partial for r in results)
    assert len(results) == 1


def test_streaming_eager_emits_partials_and_final():
    config = Config()
    config.streaming.enabled = True
    config.streaming.policy = "eager"
    config.streaming.interval_ms = 800
    backend = _FakeBackend(["hello", "hello there", "hello there friend"])
    results: list[Transcript] = []
    session = StreamingSession(backend, config, results.append)

    t = 0.0
    session.feed(_chunk(7, t, _pcm(), language="en"))
    for _ in range(20):
        t += 0.1
        session.feed(_chunk(7, t, _pcm()))
    session.feed(_chunk(7, t + 0.1, None, is_final=True))

    partials = [r for r in results if r.is_partial]
    finals = [r for r in results if not r.is_partial]

    assert len(finals) == 1
    assert finals[0].utterance_id == 7
    assert finals[0].text == "hello there friend"
    assert len(partials) == 2
    assert partials[0].text == "hello"
    assert partials[1].text == "hello there"
    assert all(p.utterance_id == 7 for p in partials)


def test_streaming_stable_only_types_committed_prefix():
    config = Config()
    config.streaming.enabled = True
    config.streaming.policy = "stable"
    config.streaming.interval_ms = 500
    backend = _FakeBackend(
        ["hello there", "hello there friend", "hello there friend how are you"]
    )
    results: list[Transcript] = []
    session = StreamingSession(backend, config, results.append)

    t = 0.0
    session.feed(_chunk(3, t, _pcm(), language="en"))
    for _ in range(12):
        t += 0.1
        session.feed(_chunk(3, t, _pcm()))
    session.feed(_chunk(3, t + 0.1, None, is_final=True))

    partials = [r for r in results if r.is_partial]

    assert partials[0].text == ""  # first pass: nothing to agree with yet
    assert partials[1].text == "hello there"  # agreement between pass 0 and pass 1


def test_streaming_window_management_trims_and_carries_prefix():
    config = Config()
    config.streaming.enabled = True
    config.streaming.policy = "eager"
    config.streaming.interval_ms = 100
    config.streaming.window_s = 1  # 16000 samples
    backend = _FakeBackend(["hello world", "continues nicely"])
    results: list[Transcript] = []
    session = StreamingSession(backend, config, results.append)

    session.feed(_chunk(9, 0.0, _pcm(8000), language="en"))
    session.feed(_chunk(9, 0.2, _pcm(100)))  # buffer=8100 < window -> decode #1
    assert len(backend.calls) == 1
    assert session._prev_pass_words == ["hello", "world"]

    session.feed(_chunk(9, 0.25, _pcm(10000)))  # buffer=18100 -> trims to 16000
    assert session._buffer.size == 16000
    assert session._prefix_text == "hello world"
    assert session._prev_pass_words == []

    session.feed(_chunk(9, 0.4, _pcm(50)))  # decode #2, carrying the trimmed prefix
    assert backend.calls[-1]["initial_prompt"] == "hello world"

    partials = [r for r in results if r.is_partial]
    assert partials[-1].text == "hello world continues nicely"


def test_streaming_emits_to_queue_sink():
    config = Config()
    config.streaming.enabled = False
    backend = _FakeBackend(["done"])
    q: queue.Queue = queue.Queue()
    session = StreamingSession(backend, config, q)

    session.feed(_chunk(2, 0.0, _pcm(), language="en"))
    session.feed(_chunk(2, 1.0, None, is_final=True))

    result = q.get_nowait()
    assert result.text == "done"
    assert result.is_partial is False


def test_streaming_rejects_chunk_from_different_utterance():
    config = Config()
    backend = _FakeBackend(["x"])
    session = StreamingSession(backend, config, lambda _t: None)
    session.feed(_chunk(1, 0.0, _pcm()))
    with pytest.raises(ValueError):
        session.feed(_chunk(2, 0.1, _pcm()))


def test_streaming_session_reusable_after_final():
    config = Config()
    config.streaming.enabled = False
    backend = _FakeBackend(["first", "second"])
    results: list[Transcript] = []
    session = StreamingSession(backend, config, results.append)

    session.feed(_chunk(1, 0.0, _pcm(), language="en"))
    session.feed(_chunk(1, 1.0, None, is_final=True))

    session.feed(_chunk(2, 2.0, _pcm(), language="de"))
    session.feed(_chunk(2, 3.0, None, is_final=True))

    assert [r.utterance_id for r in results] == [1, 2]
    assert [r.text for r in results] == ["first", "second"]
