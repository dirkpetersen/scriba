from scriba.config import Config, ConfigError, config_from_dict, load_config, save_config


def test_load_config_creates_default_file(tmp_path):
    path = tmp_path / "config.toml"
    assert not path.exists()

    config = load_config(path)

    assert path.exists()
    assert config == Config()


def test_load_config_reads_existing_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[general]\nmode = "push_to_talk"\nlanguage = "de"\n', encoding="utf-8")

    config = load_config(path)

    assert config.general.mode == "push_to_talk"
    assert config.general.language == "de"


def test_round_trip_preserves_values(tmp_path):
    path = tmp_path / "config.toml"
    config = load_config(path)
    config.general.mode = "wake_word"
    config.vad.threshold = 0.7
    config.inject.per_app["windowsterminal.exe"] = "paste"

    save_config(config, path)
    reloaded = load_config(path)

    assert reloaded.general.mode == "wake_word"
    assert reloaded.vad.threshold == 0.7
    assert reloaded.inject.per_app == {"windowsterminal.exe": "paste"}


def test_unknown_section_rejected():
    try:
        config_from_dict({"nonsense": {}})
    except ConfigError:
        pass
    else:
        raise AssertionError("expected ConfigError")


def test_unknown_key_rejected():
    try:
        config_from_dict({"general": {"mode": "toggle", "bogus": 1}})
    except ConfigError:
        pass
    else:
        raise AssertionError("expected ConfigError")


def test_invalid_mode_rejected():
    try:
        config_from_dict({"general": {"mode": "not_a_mode"}})
    except ConfigError:
        pass
    else:
        raise AssertionError("expected ConfigError")


def test_invalid_vad_threshold_rejected():
    try:
        config_from_dict({"vad": {"threshold": 1.5}})
    except ConfigError:
        pass
    else:
        raise AssertionError("expected ConfigError")
