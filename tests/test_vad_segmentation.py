"""Segmentation state machine tests (DESIGN.md §7.2): synthetic per-frame VAD
probabilities are fed directly into `UtteranceSegmenter`, with no real ONNX
model or audio hardware involved. A single narrowly-scoped `@pytest.mark.gpu`
test at the bottom exercises the real `SileroVad` ONNX wrapper (network
download required, hence the marker -- excluded by default).
"""

import numpy as np
import pytest

from scriba.config import VadConfig
from scriba.detect.vad import SileroVad, UtteranceSegmenter

DEVICE = "mic1"
FRAME_SAMPLES = 512
PREROLL = np.full(200, -1, dtype=np.int16)


def make_frame(index: int) -> np.ndarray:
    return np.full(FRAME_SAMPLES, index, dtype=np.int16)


def get_preroll_stub(device_id: str) -> np.ndarray:
    assert device_id == DEVICE
    return PREROLL


def feed(segmenter: UtteranceSegmenter, probs: list[float], t0: float = 0.0):
    """Feeds one synthetic frame per probability; returns the list of chunks
    (None entries included) in order, one per frame."""
    frame_s = 512 / 16_000
    results = []
    for i, prob in enumerate(probs):
        chunk = segmenter.process_frame(DEVICE, make_frame(i), prob, t0 + i * frame_s)
        results.append(chunk)
    return results


def test_start_end_timing_and_preroll_prepend():
    config = VadConfig(threshold=0.5, endpoint_silence_ms=160, min_speech_ms=96, pre_roll_ms=400)
    segmenter = UtteranceSegmenter(config, get_preroll_stub)
    # endpoint_frames = round(160/32) = 5, min_speech_frames = round(96/32) = 3
    probs = [0.1] * 5 + [0.9] * 10 + [0.1] * 10
    results = feed(segmenter, probs)

    non_none = [(i, c) for i, c in enumerate(results) if c is not None]
    # trigger at index 6 (frames 5,6 >= threshold); confirms once speech_span
    # reaches 3 frames, i.e. at index 7 -- then, since the utterance is now
    # open, one chunk per frame (including the trailing hangover silence)
    # until the endpoint fires after 5 consecutive sub-threshold frames,
    # closing at index 19.
    emitted_indices = [i for i, _ in non_none]
    assert emitted_indices == list(range(7, 20))

    first_chunk = non_none[0][1]
    assert first_chunk.is_final is False
    assert first_chunk.device_id == DEVICE
    # pre-roll + the 3 buffered frames (indices 5, 6, 7)
    assert len(first_chunk.pcm) == len(PREROLL) + 3 * FRAME_SAMPLES
    assert np.array_equal(first_chunk.pcm[: len(PREROLL)], PREROLL)
    assert np.all(first_chunk.pcm[len(PREROLL) : len(PREROLL) + FRAME_SAMPLES] == 5)

    utterance_ids = {c.utterance_id for _, c in non_none}
    assert utterance_ids == {1}

    final_chunks = [c for _, c in non_none if c.is_final]
    assert len(final_chunks) == 1
    assert final_chunks[0] is non_none[-1][1]

    assert segmenter.is_idle


def test_short_blip_below_min_speech_ms_is_silently_dropped():
    config = VadConfig(threshold=0.5, endpoint_silence_ms=160, min_speech_ms=96, pre_roll_ms=400)
    segmenter = UtteranceSegmenter(config, get_preroll_stub)
    # endpoint_frames = 5, min_speech_frames = 3: only 2 speech frames occur
    # before silence, so speech_span never reaches 3 -- the candidate is
    # discarded once the endpoint (5 sub-threshold frames) fires.
    probs = [0.1] * 5 + [0.9] * 2 + [0.1] * 20
    results = feed(segmenter, probs)

    assert all(chunk is None for chunk in results)
    assert segmenter.is_idle


def test_max_utterance_s_force_flush_cuts_at_next_pause_then_continues():
    config = VadConfig(
        threshold=0.5, endpoint_silence_ms=160, min_speech_ms=64, max_utterance_s=0.32
    )
    segmenter = UtteranceSegmenter(config, get_preroll_stub)
    # min_speech_frames = round(64/32) = 2 -> confirms immediately at trigger.
    # max_frames = round(0.32*1000/32) = 10.
    probs = (
        [0.9] * 12  # indices 0..11: trigger+confirm at index1, still speaking past max_frames
        + [0.1]  # index 12: first dip after the cap -> force-flush cuts here
        + [0.9] * 4  # indices 13..16: continuation utterance, still speaking
        + [0.1] * 5  # indices 17..21: real endpoint (5 sub-threshold frames) closes it
    )
    results = feed(segmenter, probs)

    non_none = [(i, c) for i, c in enumerate(results) if c is not None]
    assert [i for i, _ in non_none] == list(range(1, 13)) + list(range(13, 22))

    first_utterance = [c for i, c in non_none if i <= 12]
    second_utterance = [c for i, c in non_none if i >= 13]

    assert {c.utterance_id for c in first_utterance} == {1}
    assert {c.utterance_id for c in second_utterance} == {2}

    # force-flush cut: last chunk of utterance 1 is final despite prob==0.5 threshold not
    # being naturally endpointed (only one sub-threshold frame occurred, not a full run).
    assert first_utterance[-1].is_final is True
    assert all(not c.is_final for c in first_utterance[:-1])

    # utterance 2 closes via a real endpoint (5 consecutive sub-threshold frames).
    assert second_utterance[-1].is_final is True
    assert all(not c.is_final for c in second_utterance[:-1])

    assert segmenter.is_idle


def test_max_utterance_s_hard_cap_when_speech_never_dips():
    config = VadConfig(
        threshold=0.5, endpoint_silence_ms=160, min_speech_ms=64, max_utterance_s=0.32
    )
    segmenter = UtteranceSegmenter(config, get_preroll_stub)
    # max_frames = 10, endpoint_frames = 5 -> hard cap at frames_since_start >= 15.
    # Speech never dips, so only the hard cap can force the cut.
    probs = [0.9] * 17
    results = feed(segmenter, probs)

    finals = [(i, c) for i, c in enumerate(results) if c is not None and c.is_final]
    assert len(finals) == 1
    cut_index, cut_chunk = finals[0]
    assert cut_index == 14  # frames_since_start reaches 15 (2 at trigger + 13 more) here
    assert cut_chunk.utterance_id == 1

    after_cut = [c for i, c in enumerate(results) if c is not None and i > cut_index]
    assert after_cut  # the continuation utterance keeps emitting
    assert all(c.utterance_id == 2 for c in after_cut)
    assert not segmenter.is_idle  # still mid-utterance-2, no endpoint fed yet


@pytest.mark.gpu
def test_silero_vad_onnx_wrapper_smoke(tmp_path):
    """Downloads the real model and confirms the wrapper's shapes/API don't crash."""
    vad = SileroVad(model_path=tmp_path / "silero_vad.onnx")
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    noise = (np.random.default_rng(0).standard_normal(FRAME_SAMPLES) * 500).astype(np.int16)

    prob_silence = vad.process_frame(silence)
    prob_noise = vad.process_frame(noise)

    assert 0.0 <= prob_silence <= 1.0
    assert 0.0 <= prob_noise <= 1.0

    vad.reset()
    prob_after_reset = vad.process_frame(silence)
    assert 0.0 <= prob_after_reset <= 1.0
