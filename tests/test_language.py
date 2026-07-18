import pytest

from scriba.config import SttConfig
from scriba.stt.language import model_for_language, resolve_language


def _stt_config(**overrides) -> SttConfig:
    return SttConfig(**overrides)


def test_fixed_en_is_passthrough():
    assert resolve_language("en", None, _stt_config()) == "en"


def test_fixed_de_is_passthrough_ignores_probs():
    assert resolve_language("de", {"en": 0.99}, _stt_config()) == "de"


def test_auto_returns_none_for_no_restriction():
    assert resolve_language("auto", {"en": 0.9, "de": 0.1}, _stt_config()) is None


def test_auto_returns_none_even_without_probs():
    assert resolve_language("auto", None, _stt_config()) is None


def test_mixed_picks_highest_probability_candidate():
    stt = _stt_config(languages=["en", "de"], language_confidence_min=0.6)
    assert resolve_language("mixed", {"en": 0.3, "de": 0.7}, stt) == "de"


def test_mixed_falls_back_below_confidence_threshold():
    stt = _stt_config(languages=["en", "de"], language_confidence_min=0.6)
    assert resolve_language("mixed", {"en": 0.55, "de": 0.45}, stt) == "en"


def test_mixed_does_not_fall_back_at_exactly_the_threshold():
    # "below" is strict (<); a winning probability equal to the threshold is accepted.
    stt = _stt_config(languages=["en", "de"], language_confidence_min=0.6)
    assert resolve_language("mixed", {"en": 0.4, "de": 0.6}, stt) == "de"


def test_mixed_ignores_languages_outside_candidate_set():
    stt = _stt_config(languages=["en", "de"], language_confidence_min=0.6)
    probs = {"fr": 0.9, "en": 0.65, "de": 0.35}
    assert resolve_language("mixed", probs, stt) == "en"


def test_mixed_falls_back_when_no_candidates_present():
    stt = _stt_config(languages=["en", "de"])
    assert resolve_language("mixed", {"fr": 0.9, "es": 0.1}, stt) == "en"


def test_mixed_with_none_probs_falls_back_to_first_language():
    stt = _stt_config(languages=["de", "en"])
    assert resolve_language("mixed", None, stt) == "de"


def test_mixed_respects_custom_language_order_for_fallback():
    stt = _stt_config(languages=["de", "en"], language_confidence_min=0.6)
    assert resolve_language("mixed", {"en": 0.5, "de": 0.5}, stt) == "de"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_language("klingon", {}, _stt_config())


def test_model_for_language_english_uses_small_model():
    assert model_for_language("en") == "distil-large-v3"


def test_model_for_language_german_uses_large_model():
    assert model_for_language("de") == "large-v3-turbo"


def test_model_for_language_mixed_uses_large_model():
    assert model_for_language("mixed") == "large-v3-turbo"


def test_model_for_language_auto_uses_large_model():
    assert model_for_language("auto") == "large-v3-turbo"
