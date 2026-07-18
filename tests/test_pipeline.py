import pytest

from scriba.config import Config
from scriba.messages import ForegroundWindow, PostprocState, Transcript
from scriba.text.pipeline import run_pipeline

_WIN = ForegroundWindow(hwnd=1, title="Terminal", exe_name="WindowsTerminal.exe")


def _transcript(**overrides) -> Transcript:
    fields = {
        "text": "hello world",
        "avg_logprob": -0.2,
        "no_speech_prob": 0.1,
        "duration_s": 1.5,
        "language": "en",
        "utterance_id": 1,
        "is_partial": False,
    }
    fields.update(overrides)
    return Transcript(**fields)


def test_good_transcript_passes_through():
    jobs, state = run_pipeline(_transcript(), PostprocState(), Config(), None)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.text == "Hello world"
    assert job.erase == 0
    assert job.utterance_id == 1
    assert job.is_final is True
    assert state.capitalize_next is False
    assert state.space_before_next is True


def test_empty_text_is_dropped():
    state_in = PostprocState()
    jobs, state_out = run_pipeline(_transcript(text=""), state_in, Config(), None)

    assert jobs == []
    assert state_out == state_in


def test_whitespace_only_text_is_dropped():
    jobs, state = run_pipeline(_transcript(text="   "), PostprocState(), Config(), None)

    assert jobs == []


def test_high_no_speech_prob_is_dropped():
    jobs, _ = run_pipeline(_transcript(no_speech_prob=0.61), PostprocState(), Config(), None)

    assert jobs == []


def test_no_speech_prob_at_threshold_is_not_dropped():
    jobs, _ = run_pipeline(_transcript(no_speech_prob=0.6), PostprocState(), Config(), None)

    assert len(jobs) == 1


def test_low_avg_logprob_is_dropped():
    jobs, _ = run_pipeline(_transcript(avg_logprob=-1.01), PostprocState(), Config(), None)

    assert jobs == []


def test_avg_logprob_at_threshold_is_not_dropped():
    jobs, _ = run_pipeline(_transcript(avg_logprob=-1.0), PostprocState(), Config(), None)

    assert len(jobs) == 1


def test_too_short_duration_is_dropped():
    config = Config()
    too_short_s = (config.vad.min_speech_ms - 1) / 1000
    jobs, _ = run_pipeline(_transcript(duration_s=too_short_s), PostprocState(), config, None)

    assert jobs == []


def test_duration_at_threshold_is_not_dropped():
    config = Config()
    exactly_s = config.vad.min_speech_ms / 1000
    jobs, _ = run_pipeline(_transcript(duration_s=exactly_s), PostprocState(), config, None)

    assert len(jobs) == 1


@pytest.mark.parametrize(
    "text",
    ["Thank you.", "thank you", "you", "Thanks for watching!", "Danke.", "Untertitel"],
)
def test_blocklist_entries_are_dropped(text):
    jobs, _ = run_pipeline(_transcript(text=text), PostprocState(), Config(), None)

    assert jobs == []


def test_blocklist_extra_is_dropped():
    config = Config()
    config.postproc.blocklist_extra = ["Auf Wiedersehen"]

    jobs, _ = run_pipeline(_transcript(text="auf wiedersehen"), PostprocState(), config, None)

    assert jobs == []


def test_state_unchanged_when_dropped():
    state_in = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=42)

    jobs, state_out = run_pipeline(_transcript(text="you"), state_in, Config(), None)

    assert jobs == []
    assert state_out == state_in


def test_casing_state_carries_across_utterances():
    config = Config()
    state = PostprocState()

    jobs1, state = run_pipeline(_transcript(text="i am fine.", utterance_id=1), state, config, None)
    assert jobs1[0].text == "I am fine."

    jobs2, state = run_pipeline(
        _transcript(text="how about you", utterance_id=2), state, config, None
    )
    assert jobs2[0].text == " How about you"


def test_foreground_passed_through_resets_casing_state():
    config = Config()
    state = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=99)

    jobs, state = run_pipeline(
        _transcript(text="new window", utterance_id=2), state, config, _WIN
    )

    assert jobs[0].text == "New window"
    assert state.last_hwnd == _WIN.hwnd


def test_asserts_on_partial_transcript():
    with pytest.raises(AssertionError):
        run_pipeline(_transcript(is_partial=True), PostprocState(), Config(), None)


def test_trailing_command_applied_before_casing():
    jobs, state = run_pipeline(
        _transcript(text="see you tomorrow period"), PostprocState(), Config(), None
    )

    assert jobs[0].text == "See you tomorrow."
    assert state.capitalize_next is True
    assert state.space_before_next is True


def test_hit_enter_command_produces_newline_injectjob():
    jobs, state = run_pipeline(_transcript(text="hit enter"), PostprocState(), Config(), None)

    assert jobs[0].text == "\n"
    assert state.capitalize_next is True
    assert state.space_before_next is False


def test_filler_words_removed_by_default():
    jobs, _ = run_pipeline(
        _transcript(text="um so I think uh we should go"), PostprocState(), Config(), None
    )

    assert jobs[0].text == "So I think we should go"


def test_filler_removal_can_be_disabled():
    config = Config()
    config.postproc.filler_removal = False

    jobs, _ = run_pipeline(_transcript(text="um hello"), PostprocState(), config, None)

    assert jobs[0].text == "Um hello"


def test_transcript_that_is_only_filler_is_dropped():
    jobs, state_out = run_pipeline(_transcript(text="um"), PostprocState(), Config(), None)

    assert jobs == []
