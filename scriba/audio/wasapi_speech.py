"""Raw WASAPI capture requesting AudioCategory_Speech (DESIGN.md §7.1 deviation note).

sounddevice/PortAudio has no way to request a WASAPI stream category at all
(confirmed via its source -- WasapiSettings only exposes exclusive/
auto_convert/explicit_sample_format), so devices opened through it never get
whatever category-gated driver effects exist. Querying the WinRT
AudioCaptureEffectsManager on this machine showed AudioCategory_Speech gets
AcousticEchoCancellation + NoiseSuppression + AutomaticGainControl attached by
the driver, while AudioCategory_Other/Communications get none.

Built on pycaw's existing (already-used-elsewhere) IMMDeviceEnumerator/
IMMDevice/IAudioClient COM bindings, extended here with the two interfaces
pycaw doesn't wrap: IAudioClient2 (for SetClientProperties) and
IAudioCaptureClient (for actually reading capture buffers).
"""

import ctypes
import logging
import threading
import time
from ctypes import POINTER, Structure, byref, c_int32, c_int64, c_uint8, c_uint32, c_uint64, sizeof

import comtypes
import numpy as np
import pycaw.api.audioclient as pac
from comtypes import COMMETHOD, GUID, HRESULT
from pycaw.api.audioclient import WAVEFORMATEX, IAudioClient
from pycaw.utils import DEVICE_STATE, AudioUtilities, EDataFlow

logger = logging.getLogger(__name__)

_AUDCLNT_SHAREMODE_SHARED = 0
_AUDIO_STREAM_CATEGORY_SPEECH = 9
_AUDCLNT_BUFFERFLAGS_SILENT = 0x1
_BUFFER_DURATION_HNS = 3_000_000  # 300 ms shared-mode buffer, polled (no event handle)
_POLL_SLEEP_S = 0.01


class _AudioClientProperties(Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("bIsOffload", c_int32),
        ("eCategory", c_int32),
        ("Options", c_int32),
    ]


class _IAudioClient2(IAudioClient):
    _iid_ = GUID("{726778CD-F60A-4EDA-82DE-E47610CD78AA}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "IsOffloadCapable",
            (["in"], c_int32, "Category"),
            (["out"], POINTER(c_int32), "pbOffloadCapable"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetClientProperties",
            (["in"], POINTER(_AudioClientProperties), "pProperties"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetBufferSizeLimits",
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["in"], c_int32, "bEventDriven"),
            (["out"], POINTER(c_int64), "phnsMinBufferDuration"),
            (["out"], POINTER(c_int64), "phnsMaxBufferDuration"),
        ),
    )


class _IAudioCaptureClient(pac.IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(c_uint8)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(c_uint32), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD(
            [], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")
        ),
    )


def find_capture_device(name: str):
    """Match a sounddevice-reported friendly name to its WASAPI IMMDevice."""
    for dev in AudioUtilities.GetAllDevices(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value):
        if dev is not None and dev.FriendlyName == name:
            return dev
    return None


class WasapiSpeechStream:
    """Opt-in alternative to sounddevice.InputStream for one device.

    Two-phase like sounddevice's stream objects: construct, `open()` (raises
    on failure -- caller decides whether to fall back), then `start(callback)`
    once the real samplerate is known. `callback` gets the same
    `(indata, frames, time_info, status)` shape AudioCapture's own callback
    uses, with `indata` as `(frames, 1)` int16 -- so it drops into the
    existing resample/FrameChunker/RingBuffer pipeline unchanged.
    """

    def __init__(self, name: str):
        self._name = name
        self._callback = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        self._capture_client = None
        self._bits = None
        self.samplerate: int | None = None
        self.channels: int | None = None

    def open(self) -> None:
        comtypes.CoInitialize()
        dev = find_capture_device(self._name)
        if dev is None:
            raise RuntimeError(f"no WASAPI capture endpoint matching {self._name!r}")

        client = dev._dev.Activate(_IAudioClient2._iid_, comtypes.CLSCTX_ALL, None)
        client = client.QueryInterface(_IAudioClient2)

        props = _AudioClientProperties()
        props.cbSize = sizeof(_AudioClientProperties)
        props.bIsOffload = 0
        props.eCategory = _AUDIO_STREAM_CATEGORY_SPEECH
        props.Options = 0
        client.SetClientProperties(byref(props))

        fmt_ptr = client.GetMixFormat()
        fmt = fmt_ptr.contents
        client.Initialize(_AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION_HNS, 0, fmt_ptr, None)
        capture_client = client.GetService(_IAudioCaptureClient._iid_)
        capture_client = capture_client.QueryInterface(_IAudioCaptureClient)

        self._client = client
        self._capture_client = capture_client
        self.channels = fmt.nChannels
        self.samplerate = fmt.nSamplesPerSec
        self._bits = fmt.wBitsPerSample

    def start(self, callback) -> None:
        self._callback = callback
        self._client.Start()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        dtype = np.float32 if self._bits == 32 else np.int16
        bytes_per_sample = self._bits // 8
        while not self._stop_event.is_set():
            time.sleep(_POLL_SLEEP_S)
            try:
                pkt = self._capture_client.GetNextPacketSize()
                while pkt > 0:
                    data_ptr, n_frames, flags, _, _ = self._capture_client.GetBuffer()
                    if n_frames > 0:
                        frame = self._to_frame(data_ptr, n_frames, flags, dtype, bytes_per_sample)
                        self._callback(frame, n_frames, None, None)
                    self._capture_client.ReleaseBuffer(n_frames)
                    pkt = self._capture_client.GetNextPacketSize()
            except Exception:
                logger.exception("WASAPI-Speech poll loop error for %s", self._name)
                break

    def _to_frame(self, data_ptr, n_frames, flags, dtype, bytes_per_sample):
        if flags & _AUDCLNT_BUFFERFLAGS_SILENT:
            return np.zeros((n_frames, 1), dtype=np.int16)
        n_bytes = n_frames * self.channels * bytes_per_sample
        buf = ctypes.string_at(data_ptr, n_bytes)
        arr = np.frombuffer(buf, dtype=dtype).reshape(-1, self.channels)
        mono = arr.mean(axis=1) if self.channels > 1 else arr[:, 0]
        if dtype == np.float32:
            mono = np.clip(mono * 32768.0, -32768, 32767)
        return mono.astype(np.int16).reshape(-1, 1)

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                logger.exception("error stopping WASAPI-Speech client for %s", self._name)
        self._client = None
        self._capture_client = None
