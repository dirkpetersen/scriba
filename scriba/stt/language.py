"""Language policy: fixed/auto/mixed resolution (DESIGN.md §7.10(b)).

Pure function, no model calls. `probs` is a plain `dict[str, float]` of
language code -> probability - the shape produced by
`WhisperLocalBackend.detect_language_probs()` (which just turns
`faster_whisper`'s `WhisperModel.detect_language()` `all_language_probs`
list of tuples into a dict). Keeping this a pure function of that dict (and
the config) makes it fully unit-testable without a model.
"""

from ..config import SttConfig


def resolve_language(
    mode: str,
    probs: dict[str, float] | None,
    stt_config: SttConfig,
) -> str | None:
    """Resolves `general.language` (`mode`) to the language code for `SttBackend.transcribe()`.

    - `"en"` / `"de"`: fixed, trivial passthrough - `probs` is ignored.
    - `"auto"`: no restriction - returns `None` so the backend lets Whisper's
      own open-set detection run at transcribe() time (DESIGN §7.10(b)).
    - `"mixed"`: restricts the argmax of `probs` to `stt_config.languages`;
      falls back to `stt_config.languages[0]` if the winning candidate's
      probability is below `stt_config.language_confidence_min`, or if none
      of the candidate languages appear in `probs` at all.
    """
    if mode in ("en", "de"):
        return mode
    if mode == "auto":
        return None
    if mode == "mixed":
        candidates = {lang: p for lang, p in (probs or {}).items() if lang in stt_config.languages}
        if not candidates:
            return stt_config.languages[0]
        best_lang, best_prob = max(candidates.items(), key=lambda kv: kv[1])
        if best_prob < stt_config.language_confidence_min:
            return stt_config.languages[0]
        return best_lang
    raise ValueError(f"unknown language mode: {mode!r}")


def model_for_language(language: str) -> str:
    """Which STT model `general.language` requires (user-requested design change).

    `distil-large-v3` is English-only (DESIGN §3) -- not a preference, a hard
    requirement -- so anything other than plain `"en"` (German, mixed, or
    open-set auto, which might not be English at all) must use the
    multilingual `large-v3-turbo`. This is the single source of truth for
    that mapping; callers should treat `stt.model` as derived from
    `general.language`, not independently user-selectable.
    """
    return "distil-large-v3" if language == "en" else "large-v3-turbo"
