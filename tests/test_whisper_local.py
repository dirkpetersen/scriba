"""Tests for the pure, hardware-free helpers in whisper_local.py. The model
fallback ladder / actual transcription need a real GPU+model and are covered
manually / by @pytest.mark.gpu smoke tests elsewhere, not here.
"""

import numpy as np

from scriba.stt.whisper_local import _denoise, _is_cuda_oom, _to_float32

_SAMPLE_RATE = 16000


def test_to_float32_scales_int16_into_unit_range():
    pcm = np.array([-32768, 0, 32767], dtype=np.int16)

    audio = _to_float32(pcm)

    assert audio.dtype == np.float32
    assert audio[0] == -1.0
    assert audio[1] == 0.0
    assert 0.99 < audio[2] < 1.0


def test_to_float32_passes_through_existing_float32():
    audio_in = np.array([0.1, -0.2, 0.3], dtype=np.float32)

    assert _to_float32(audio_in) is audio_in


def test_is_cuda_oom_matches_out_of_memory_message():
    assert _is_cuda_oom(RuntimeError("CUDA out of memory. Tried to allocate ..."))
    assert _is_cuda_oom(RuntimeError("CUBLAS_STATUS_ALLOC_FAILED: out of memory"))


def test_is_cuda_oom_does_not_match_unrelated_errors():
    assert not _is_cuda_oom(RuntimeError("model file not found"))
    assert not _is_cuda_oom(ValueError("invalid language code"))


def _tone(
    freq_hz: float, duration_s: float, amplitude: float, sr: int = _SAMPLE_RATE
) -> np.ndarray:
    t = np.arange(int(duration_s * sr)) / sr
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def test_denoise_reduces_pure_noise_energy():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.05, _SAMPLE_RATE * 2).astype(np.float32)

    denoised = _denoise(noise)

    assert denoised.dtype == np.float32
    assert denoised.shape == noise.shape
    # pure noise has no stable spectral profile to preserve, so gating should
    # visibly attenuate it rather than pass it through unchanged
    assert np.sqrt(np.mean(denoised**2)) < np.sqrt(np.mean(noise**2)) * 0.9


def test_denoise_increases_contrast_between_loud_and_quiet_segments():
    """A crude proxy for "improves intelligibility over background noise".

    A perfectly stationary pure tone is exactly the degenerate case spectral
    gating handles badly (tried first: it reads as "noise" itself and gets
    gated to near-silence, since it has no bursty/non-stationary structure
    to distinguish it from a hum). Real speech is bursty, so this uses
    alternating loud/quiet segments and checks that denoising increases the
    relative energy contrast between them, rather than asserting exact
    waveform preservation -- perceptual audio quality is something only
    manual listening can really judge.
    """
    rng = np.random.default_rng(2)
    sr = _SAMPLE_RATE
    quiet = rng.normal(0, 0.03, sr).astype(np.float32)
    loud = _tone(200, 1.0, 0.4) + rng.normal(0, 0.03, sr).astype(np.float32)
    noisy = np.concatenate([quiet, loud, quiet, loud, quiet]).astype(np.float32)

    denoised = _denoise(noisy)

    def _segment_rms(arr: np.ndarray, segment_index: int) -> float:
        segment = arr[segment_index * sr : (segment_index + 1) * sr]
        return float(np.sqrt(np.mean(segment**2)))

    original_contrast = _segment_rms(noisy, 1) / (_segment_rms(noisy, 0) + 1e-9)
    denoised_contrast = _segment_rms(denoised, 1) / (_segment_rms(denoised, 0) + 1e-9)
    assert denoised_contrast > original_contrast
