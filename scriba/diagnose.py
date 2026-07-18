"""`--diagnose` support (DESIGN.md §7.11).

Deliberately self-contained: queries sounddevice/ctranslate2/onnxruntime and
`scriba.config` directly rather than importing scriba.audio/detect/stt, none
of which this module depends on. A future `scriba/app.py --diagnose` flag
calls `run_diagnostics()`.
"""

from dataclasses import dataclass, field
from pathlib import Path

import ctranslate2
import onnxruntime as ort
import sounddevice as sd

from .config import models_dir


@dataclass
class InputDeviceInfo:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float
    hostapi_name: str


@dataclass
class CudaInfo:
    cuda_device_count: int
    cuda_available: bool
    supported_compute_types_cpu: set[str]
    supported_compute_types_cuda: set[str] | None
    onnxruntime_providers: list[str]
    onnxruntime_cuda_available: bool
    error: str | None = None


@dataclass
class ModelCacheInfo:
    path: Path
    exists: bool
    entries: list[str] = field(default_factory=list)


def list_input_devices() -> list[InputDeviceInfo]:
    """Input-capable devices (sounddevice/PortAudio), each with its default sample rate."""
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    result = []
    for d in devices:
        if d["max_input_channels"] > 0:
            result.append(
                InputDeviceInfo(
                    index=d["index"],
                    name=d["name"],
                    max_input_channels=d["max_input_channels"],
                    default_samplerate=d["default_samplerate"],
                    hostapi_name=hostapis[d["hostapi"]]["name"],
                )
            )
    return result


def check_cuda() -> CudaInfo:
    """CUDA/cuDNN/cuBLAS resolution via ctranslate2 (faster-whisper's engine), plus onnxruntime."""
    error = None
    cuda_device_count = 0
    supported_cuda: set[str] | None = None

    try:
        cuda_device_count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # pragma: no cover -- defensive, environment-dependent
        error = f"ctranslate2.get_cuda_device_count() failed: {exc}"

    if cuda_device_count > 0:
        try:
            supported_cuda = ctranslate2.get_supported_compute_types("cuda")
        except Exception as exc:  # pragma: no cover -- defensive, environment-dependent
            detail = f"get_supported_compute_types('cuda') failed: {exc}"
            error = f"{error}; {detail}" if error else detail

    supported_cpu = ctranslate2.get_supported_compute_types("cpu")
    providers = ort.get_available_providers()

    return CudaInfo(
        cuda_device_count=cuda_device_count,
        cuda_available=cuda_device_count > 0,
        supported_compute_types_cpu=supported_cpu,
        supported_compute_types_cuda=supported_cuda,
        onnxruntime_providers=providers,
        onnxruntime_cuda_available="CUDAExecutionProvider" in providers,
        error=error,
    )


def model_cache_state(path: Path | None = None) -> ModelCacheInfo:
    """Whether `scriba.config.models_dir()` exists and what's cached there (no downloads)."""
    path = path or models_dir()
    if not path.exists():
        return ModelCacheInfo(path=path, exists=False, entries=[])
    entries = sorted(p.name for p in path.iterdir())
    return ModelCacheInfo(path=path, exists=True, entries=entries)


def _print_devices(devices: list[InputDeviceInfo]) -> None:
    print("-- Input devices (sounddevice/PortAudio) --")
    if not devices:
        print("  No input-capable devices found.")
        return
    for d in devices:
        print(
            f"  [{d.index:>3}] {d.name!r:45s} "
            f"channels={d.max_input_channels}  default_sr={d.default_samplerate:.0f} Hz  "
            f"hostapi={d.hostapi_name}"
        )


def _print_cuda(info: CudaInfo) -> None:
    print("-- CUDA / cuDNN / cuBLAS (ctranslate2, backs faster-whisper) --")
    if info.cuda_available:
        print(f"  CUDA devices visible: {info.cuda_device_count}")
        cuda_types = sorted(info.supported_compute_types_cuda or [])
        print(f"  Supported compute types on cuda: {cuda_types}")
    else:
        print("  CUDA devices visible: 0")
        print(
            "  faster-whisper will NOT be able to use the GPU rung (DESIGN §9 rung 1) -- "
            "check `nvidia-smi`, the NVIDIA driver, and that the nvidia-cudnn-cu12 / "
            "nvidia-cublas-cu12 pip wheels installed cleanly."
        )
    print(f"  Supported compute types on cpu: {sorted(info.supported_compute_types_cpu)}")
    if info.error:
        print(f"  NOTE: {info.error}")

    print(f"  onnxruntime providers: {info.onnxruntime_providers}")
    if info.onnxruntime_cuda_available:
        print("  onnxruntime CUDAExecutionProvider: available")
    else:
        print(
            "  onnxruntime CUDAExecutionProvider: not available -- expected per DESIGN.md "
            "(Silero VAD / openWakeWord run onnxruntime on CPU only; this does not affect "
            "faster-whisper's CUDA path above)."
        )


def _print_model_cache(info: ModelCacheInfo) -> None:
    print(f"-- Model cache ({info.path}) --")
    if not info.exists:
        print("  Directory does not exist yet -- no models downloaded.")
        return
    if not info.entries:
        print("  Directory exists but is empty.")
        return
    for entry in info.entries:
        print(f"  {entry}")


def run_diagnostics() -> None:
    """Print the `--diagnose` report (DESIGN.md §7.11) to stdout."""
    print("=== Scriba diagnostics ===")
    print()
    _print_devices(list_input_devices())
    print()
    _print_cuda(check_cuda())
    print()
    _print_model_cache(model_cache_state())
    print()
    print("-- STT benchmark --")
    print("  run STT benchmark: not yet wired (needs scriba.stt, added during integration)")
