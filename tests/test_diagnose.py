from scriba.diagnose import (
    CudaInfo,
    InputDeviceInfo,
    check_cuda,
    list_input_devices,
    model_cache_state,
    run_diagnostics,
)


def test_list_input_devices_returns_list_of_input_device_info():
    devices = list_input_devices()
    assert isinstance(devices, list)
    for d in devices:
        assert isinstance(d, InputDeviceInfo)
        assert d.max_input_channels > 0


def test_check_cuda_does_not_raise_and_reports_consistent_shape():
    info = check_cuda()
    assert isinstance(info, CudaInfo)
    assert info.cuda_available == (info.cuda_device_count > 0)
    assert isinstance(info.supported_compute_types_cpu, set)
    assert isinstance(info.onnxruntime_providers, list)
    assert info.onnxruntime_cuda_available == (
        "CUDAExecutionProvider" in info.onnxruntime_providers
    )


def test_model_cache_state_missing_dir(tmp_path):
    missing = tmp_path / "models"
    info = model_cache_state(missing)
    assert info.exists is False
    assert info.entries == []
    assert info.path == missing


def test_model_cache_state_lists_entries(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "large-v3-turbo").mkdir()
    (models / "README.txt").write_text("x", encoding="utf-8")

    info = model_cache_state(models)

    assert info.exists is True
    assert info.entries == ["README.txt", "large-v3-turbo"]


def test_run_diagnostics_prints_report(capsys):
    run_diagnostics()
    out = capsys.readouterr().out
    assert "Scriba diagnostics" in out
    assert "Input devices" in out
    assert "CUDA" in out
    assert "Model cache" in out
    assert "STT benchmark" in out
