"""Ordered pure text post-processing pipeline (DESIGN.md §7.5).

`(Transcript, PostprocState, config, foreground: ForegroundWindow | None) ->
(list[InjectJob], PostprocState)` -- no I/O, no globals (DESIGN §7.5
deviation note: `foreground` is threaded in explicitly by the caller so the
"reset on window change" rule stays a pure comparison).

Steps 1 (hallucination filter), 2 (spoken commands), 3 (filler removal), and
5 (casing/spacing state machine) are implemented. Step 4 (vocabulary
correction) and the umlaut-fold option in step 6 remain M2 work -- they need
the vocabulary.txt system, which doesn't exist yet -- see docs/PLAN.md.

`run_pipeline` is meant to run on **final** transcripts only
(`Transcript.is_partial is False`); streaming partials bypass this pipeline
per DESIGN §7.4a.
"""

import logging
import re

from ..config import Config
from ..messages import ForegroundWindow, InjectJob, PostprocState, Transcript
from .casing import apply_casing
from .commands import apply_commands

logger = logging.getLogger(__name__)

# DESIGN §7.5 point 3: filler words, EN + DE, stripped word-initial and
# mid-sentence. Kept to the design doc's own examples (plus obvious
# spelling variants) rather than broader lists like "like"/"you know"/"also",
# which are real words far more often than they're fillers.
_FILLER_WORDS = ("um", "umm", "uh", "uhh", "hm", "hmm", "ah", "er", "erm", "äh", "ähm", "ähh")
_FILLER_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _FILLER_WORDS) + r")\b,?", re.IGNORECASE
)


def _remove_fillers(text: str) -> str:
    cleaned = _FILLER_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()

# Whisper's classic silence/subtitle hallucinations, EN + DE (DESIGN §7.5
# point 1). Extensible via config.postproc.blocklist_extra. Compared
# case-insensitively against the stripped transcript text.
_BLOCKLIST = {
    "thank you.",
    "thank you",
    "thanks for watching!",
    "thanks for watching",
    "thanks for watching.",
    "you",
    "bye.",
    "bye",
    "bye bye.",
    "please subscribe",
    "subscribe to my channel",
    "subtitles by the amara.org community",
    "danke.",
    "danke",
    "vielen dank.",
    "vielen dank",
    "vielen dank fürs zuschauen",
    "untertitel",
    "untertitelung",
}

_NO_SPEECH_PROB_MAX = 0.6
_AVG_LOGPROB_MIN = -1.0


def _hallucination_reason(transcript: Transcript, config: Config) -> str | None:
    """Returns the reason this transcript should be dropped, or None to keep it."""
    text = transcript.text.strip()
    if not text:
        return "empty text"
    if transcript.no_speech_prob > _NO_SPEECH_PROB_MAX:
        return f"no_speech_prob {transcript.no_speech_prob:.2f} > {_NO_SPEECH_PROB_MAX}"
    if transcript.avg_logprob < _AVG_LOGPROB_MIN:
        return f"avg_logprob {transcript.avg_logprob:.2f} < {_AVG_LOGPROB_MIN}"
    if transcript.duration_s * 1000 < config.vad.min_speech_ms:
        return f"duration {transcript.duration_s * 1000:.0f} ms < {config.vad.min_speech_ms} ms"
    blocklist = _BLOCKLIST | {entry.strip().lower() for entry in config.postproc.blocklist_extra}
    if text.lower() in blocklist:
        return "blocklist match"
    return None


def run_pipeline(
    transcript: Transcript,
    state: PostprocState,
    config: Config,
    foreground: ForegroundWindow | None,
) -> tuple[list[InjectJob], PostprocState]:
    """Run the M1 post-processing pipeline on a final transcript.

    Returns `([], state)` unchanged if the transcript is dropped as a
    hallucination. Otherwise returns a single `InjectJob` with the cased/
    spaced text and the `PostprocState` to carry into the next utterance.
    """
    assert not transcript.is_partial, "run_pipeline only runs on final transcripts (DESIGN §7.4a)"

    reason = _hallucination_reason(transcript, config)
    if reason is not None:
        logger.info("dropped transcript (%s): %r", reason, transcript.text)
        return [], state

    # Filler removal runs BEFORE commands (reversing DESIGN §7.5's listed
    # order): its whitespace cleanup collapses newlines, which would destroy
    # a "new line"/"new paragraph"/"hit enter" command's \n(\n) output if it
    # ran after. Fillers never appear inside a command phrase, so running it
    # first is equivalent for every other case and avoids the conflict.
    text = transcript.text.strip()
    if config.postproc.filler_removal:
        text = _remove_fillers(text)
    text = apply_commands(text)
    if not text:
        logger.info("dropped transcript (empty after commands/filler removal): %r", transcript.text)
        return [], state

    text, next_state = apply_casing(text, state, foreground)
    job = InjectJob(text=text, erase=0, utterance_id=transcript.utterance_id, is_final=True)
    return [job], next_state
