from pathlib import Path

import pytest

from scriba.autostart import autostart_command


def test_autostart_command_uses_uv_path_and_repo_root():
    command = autostart_command(repo_root=Path("C:/repo"), uv_path="C:/tools/uv.exe")

    assert command == '"C:/tools/uv.exe" run --project "C:\\repo" scriba'


def test_autostart_command_raises_without_uv():
    with pytest.raises(RuntimeError):
        autostart_command(repo_root=Path("C:/repo"), uv_path=None)
