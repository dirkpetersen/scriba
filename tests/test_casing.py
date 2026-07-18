from scriba.messages import ForegroundWindow, PostprocState
from scriba.text.casing import apply_casing

_WIN_A = ForegroundWindow(hwnd=100, title="Terminal", exe_name="WindowsTerminal.exe")
_WIN_B = ForegroundWindow(hwnd=200, title="Notepad", exe_name="notepad.exe")


def test_initial_state_capitalizes_no_leading_space():
    text, state = apply_casing("hello world", PostprocState(), None)

    assert text == "Hello world"
    assert state.capitalize_next is False
    assert state.space_before_next is True
    assert state.last_hwnd is None


def test_mid_sentence_lowercases_and_prepends_space():
    state = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=None)

    text, next_state = apply_casing("How are you", state, None)

    assert text == " how are you"
    assert next_state.capitalize_next is False
    assert next_state.space_before_next is True


def test_sentence_end_period_capitalizes_next_and_adds_space():
    state = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=None)

    text, next_state = apply_casing("i am fine.", state, None)

    assert text == " i am fine."
    assert next_state.capitalize_next is True
    assert next_state.space_before_next is True


def test_sentence_end_question_mark_capitalizes_next():
    state = PostprocState(capitalize_next=True, space_before_next=True, last_hwnd=None)

    text, next_state = apply_casing("are you sure?", state, None)

    assert text == " Are you sure?"
    assert next_state.capitalize_next is True
    assert next_state.space_before_next is True


def test_sentence_end_exclamation_mark_capitalizes_next():
    state = PostprocState(capitalize_next=True, space_before_next=True, last_hwnd=None)

    text, next_state = apply_casing("watch out!", state, None)

    assert next_state.capitalize_next is True
    assert next_state.space_before_next is True


def test_newline_capitalizes_next_without_leading_space():
    state = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=None)

    text, next_state = apply_casing("new paragraph\n", state, None)

    assert text == " new paragraph\n"
    assert next_state.capitalize_next is True
    assert next_state.space_before_next is False


def test_full_multi_utterance_sequence():
    state = PostprocState()

    text1, state = apply_casing("hello there", state, None)
    assert text1 == "Hello there"

    text2, state = apply_casing("how are you", state, None)
    assert text2 == " how are you"

    text3, state = apply_casing("i am fine.", state, None)
    assert text3 == " i am fine."

    text4, state = apply_casing("what about you", state, None)
    assert text4 == " What about you"


def test_reset_on_foreground_window_change():
    state = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=_WIN_A.hwnd)

    text, next_state = apply_casing("new window text", state, _WIN_B)

    assert text == "New window text"
    assert next_state.last_hwnd == _WIN_B.hwnd


def test_no_reset_when_same_foreground_window():
    _, state = apply_casing("first", PostprocState(last_hwnd=_WIN_A.hwnd), _WIN_A)

    text, next_state = apply_casing("second", state, _WIN_A)

    assert text == " second"
    assert next_state.last_hwnd == _WIN_A.hwnd


def test_reset_from_some_window_to_none_foreground():
    state = PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=_WIN_A.hwnd)

    text, next_state = apply_casing("no window now", state, None)

    assert text == "No window now"
    assert next_state.last_hwnd is None


def test_last_hwnd_updates_every_call_even_on_empty_text():
    text, next_state = apply_casing("", PostprocState(last_hwnd=None), _WIN_A)

    assert text == ""
    assert next_state.last_hwnd == _WIN_A.hwnd
    assert next_state.capitalize_next is True
    assert next_state.space_before_next is False


def test_apply_casing_does_not_mutate_input_state():
    state = PostprocState(capitalize_next=True, space_before_next=False, last_hwnd=None)

    apply_casing("hello", state, None)

    assert state.capitalize_next is True
    assert state.space_before_next is False
    assert state.last_hwnd is None


def test_unicode_first_letter_capitalization():
    text, _ = apply_casing("ähm das ist gut", PostprocState(), None)

    assert text == "Ähm das ist gut"
