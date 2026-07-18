import queue

import numpy as np
import pytest

from scriba.audio import capture
from scriba.audio.capture import (
    AudioCapture,
    DeviceInfo,
    RingBuffer,
    _device_id,
    list_devices,
    resample_to_16k,
)
from scriba.config import Config

# --- RingBuffer ---------------------------------------------------------


def test_ring_buffer_not_yet_full_returns_only_pushed_samples():
    buf = RingBuffer(10)
    buf.push(np.array([1, 2, 3, 4], dtype=np.int16))

    result = buf.read()

    assert result.dtype == np.int16
    np.testing.assert_array_equal(result, [1, 2, 3, 4])


def test_ring_buffer_empty_returns_empty_array():
    buf = RingBuffer(10)

    result = buf.read()

    assert len(result) == 0
    assert result.dtype == np.int16


def test_ring_buffer_wraps_across_multiple_pushes():
    buf = RingBuffer(5)
    buf.push(np.array([1, 2, 3], dtype=np.int16))
    buf.push(np.array([4, 5, 6, 7], dtype=np.int16))

    result = buf.read()

    np.testing.assert_array_equal(result, [3, 4, 5, 6, 7])


def test_ring_buffer_single_push_larger_than_capacity():
    buf = RingBuffer(5)
    buf.push(np.arange(1, 11, dtype=np.int16))  # 1..10

    result = buf.read()

    np.testing.assert_array_equal(result, [6, 7, 8, 9, 10])


def test_ring_buffer_exact_capacity_fill():
    buf = RingBuffer(4)
    buf.push(np.array([1, 2], dtype=np.int16))
    buf.push(np.array([3, 4], dtype=np.int16))

    result = buf.read()

    np.testing.assert_array_equal(result, [1, 2, 3, 4])


def test_ring_buffer_zero_capacity_never_crashes():
    buf = RingBuffer(0)
    buf.push(np.array([1, 2, 3], dtype=np.int16))

    result = buf.read()

    assert len(result) == 0


def test_ring_buffer_many_small_pushes_wrap_correctly():
    buf = RingBuffer(6)
    for sample in range(1, 13):  # 1..12, one at a time
        buf.push(np.array([sample], dtype=np.int16))

    result = buf.read()

    np.testing.assert_array_equal(result, [7, 8, 9, 10, 11, 12])


# --- resample_to_16k -----------------------------------------------------


def test_resample_passthrough_at_target_rate():
    pcm = np.array([100, -200, 300], dtype=np.int16)

    result = resample_to_16k(pcm, 16000)

    assert result.dtype == np.int16
    np.testing.assert_array_equal(result, pcm)


def test_resample_downsamples_48k_to_16k():
    pcm = np.full(480, 1000, dtype=np.int16)  # 10 ms @ 48 kHz

    result = resample_to_16k(pcm, 48000)

    assert result.dtype == np.int16
    assert len(result) == 160  # 10 ms @ 16 kHz


def test_resample_upsamples_8k_to_16k():
    pcm = np.full(100, 500, dtype=np.int16)

    result = resample_to_16k(pcm, 8000)

    assert result.dtype == np.int16
    assert len(result) == 200


def test_resample_preserves_amplitude_of_constant_signal():
    pcm = np.full(480, 1000, dtype=np.int16)

    result = resample_to_16k(pcm, 48000)

    assert abs(int(np.median(result)) - 1000) < 5


def test_resample_clips_to_int16_range():
    pcm = np.full(480, 32767, dtype=np.int16)

    result = resample_to_16k(pcm, 44100)

    assert result.max() <= 32767
    assert result.min() >= -32768


# --- device_id derivation -------------------------------------------------


def test_device_id_deterministic_for_same_name():
    assert _device_id("Microphone (HD Pro Webcam C920)") == _device_id(
        "Microphone (HD Pro Webcam C920)"
    )


def test_device_id_differs_for_different_names():
    assert _device_id("Microphone A") != _device_id("Microphone B")


def test_device_id_strips_whitespace_before_hashing():
    assert _device_id("Headset Mic") == _device_id("  Headset Mic  ")


def test_device_id_is_nonempty_string():
    device_id = _device_id("Some Device")

    assert isinstance(device_id, str)
    assert len(device_id) > 0


# --- list_devices (pure enumeration/enabled logic, sounddevice mocked) ----


class _FakeDefault:
    def __init__(self, device):
        self.device = device


@pytest.fixture
def fake_sounddevice(monkeypatch):
    hostapis = (
        {"name": "MME"},
        {"name": "Windows WASAPI"},
    )
    devices = [
        {"name": "Laptop Mic", "index": 0, "hostapi": 0, "max_input_channels": 2,
         "default_samplerate": 44100.0},
        {"name": "Laptop Mic", "index": 5, "hostapi": 1, "max_input_channels": 2,
         "default_samplerate": 16000.0},
        {"name": "USB Headset", "index": 1, "hostapi": 0, "max_input_channels": 1,
         "default_samplerate": 48000.0},
        {"name": "Speakers", "index": 2, "hostapi": 0, "max_input_channels": 0,
         "default_samplerate": 44100.0},
    ]

    def query_devices(idx=None):
        if idx is None:
            return devices
        for d in devices:
            if d["index"] == idx:
                return d
        raise ValueError("no such device")

    monkeypatch.setattr(capture.sd, "query_hostapis", lambda: hostapis)
    monkeypatch.setattr(capture.sd, "query_devices", query_devices)
    monkeypatch.setattr(capture.sd, "default", _FakeDefault([0, 2]))
    return devices


def test_list_devices_dedupes_by_name_preferring_wasapi(fake_sounddevice):
    devices = list_devices()

    laptop_mic_entries = [d for d in devices if d.name == "Laptop Mic"]
    assert len(laptop_mic_entries) == 1


def test_list_devices_excludes_output_only_devices(fake_sounddevice):
    devices = list_devices()

    assert all(d.name != "Speakers" for d in devices)


def test_list_devices_empty_enabled_list_means_all_enabled(fake_sounddevice):
    devices = list_devices([])

    assert all(d.enabled for d in devices)


def test_list_devices_enabled_set_filters_by_name(fake_sounddevice):
    devices = list_devices(["USB Headset"])

    by_name = {d.name: d.enabled for d in devices}
    assert by_name["USB Headset"] is True
    assert by_name["Laptop Mic"] is False


def test_list_devices_marks_default_device(fake_sounddevice):
    devices = list_devices()

    by_name = {d.name: d.is_default for d in devices}
    assert by_name["Laptop Mic"] is True
    assert by_name["USB Headset"] is False


def test_list_devices_ids_match_device_id_helper(fake_sounddevice):
    devices = list_devices()

    for d in devices:
        assert d.id == _device_id(d.name)


def test_device_info_fields():
    info = DeviceInfo(id="abc123", name="Mic", is_default=True, enabled=True)

    assert info.id == "abc123"
    assert info.name == "Mic"
    assert info.is_default is True
    assert info.enabled is True


class TestFrameChunker:
    def test_exact_multiple_passes_through(self):
        from scriba.audio.capture import FrameChunker

        chunker = FrameChunker(frame_samples=512)
        frames = chunker.push(np.arange(1024, dtype=np.int16))
        assert [len(f) for f in frames] == [512, 512]

    def test_odd_lengths_accumulate_without_loss(self):
        from scriba.audio.capture import FrameChunker

        # the 22050 Hz case: resample yields 513 samples per callback
        chunker = FrameChunker(frame_samples=512)
        total_in = 0
        total_out = 0
        for i in range(100):
            samples = np.full(513, i, dtype=np.int16)
            total_in += len(samples)
            for frame in chunker.push(samples):
                assert len(frame) == 512
                total_out += len(frame)
        # everything emitted except the final incomplete residual
        assert total_in - total_out < 512

    def test_preserves_sample_order(self):
        from scriba.audio.capture import FrameChunker

        chunker = FrameChunker(frame_samples=4)
        out = []
        parts = (np.array([0, 1, 2], dtype=np.int16), np.array([3, 4, 5, 6, 7], dtype=np.int16))
        for chunk in parts:
            out.extend(chunker.push(chunk))
        assert np.concatenate(out).tolist() == [0, 1, 2, 3, 4, 5, 6, 7]


# --- _open_device: WASAPI Speech-category opt-in wiring (no real hardware) --


def _make_capture(wasapi_speech_category: bool) -> AudioCapture:
    config = Config()
    config.audio.wasapi_speech_category = wasapi_speech_category
    return AudioCapture(config, queue.Queue())


_WASAPI_ENTRY = {"name": "Mic", "index": 0, "default_samplerate": 16000.0, "_is_wasapi": True}
_NON_WASAPI_ENTRY = {"name": "Mic", "index": 0, "default_samplerate": 16000.0, "_is_wasapi": False}


def test_open_device_prefers_wasapi_speech_when_enabled_and_available(monkeypatch):
    cap = _make_capture(wasapi_speech_category=True)
    monkeypatch.setattr(cap, "_try_open_wasapi_speech", lambda *a, **k: "WASAPI_STREAM")
    monkeypatch.setattr(cap, "_try_open", lambda *a, **k: pytest.fail("should not fall back"))

    cap._open_device("dev1", _WASAPI_ENTRY)

    assert cap._streams["dev1"] == "WASAPI_STREAM"


def test_open_device_falls_back_to_sounddevice_when_wasapi_open_fails(monkeypatch):
    cap = _make_capture(wasapi_speech_category=True)
    monkeypatch.setattr(cap, "_try_open_wasapi_speech", lambda *a, **k: None)
    monkeypatch.setattr(cap, "_try_open", lambda *a, **k: "SD_STREAM")

    cap._open_device("dev1", _WASAPI_ENTRY)

    assert cap._streams["dev1"] == "SD_STREAM"


def test_open_device_skips_wasapi_when_config_disabled(monkeypatch):
    cap = _make_capture(wasapi_speech_category=False)
    monkeypatch.setattr(
        cap, "_try_open_wasapi_speech", lambda *a, **k: pytest.fail("should be skipped")
    )
    monkeypatch.setattr(cap, "_try_open", lambda *a, **k: "SD_STREAM")

    cap._open_device("dev1", _WASAPI_ENTRY)

    assert cap._streams["dev1"] == "SD_STREAM"


def test_open_device_skips_wasapi_when_entry_not_wasapi(monkeypatch):
    cap = _make_capture(wasapi_speech_category=True)
    monkeypatch.setattr(
        cap, "_try_open_wasapi_speech", lambda *a, **k: pytest.fail("should be skipped")
    )
    monkeypatch.setattr(cap, "_try_open", lambda *a, **k: "SD_STREAM")

    cap._open_device("dev1", _NON_WASAPI_ENTRY)

    assert cap._streams["dev1"] == "SD_STREAM"
