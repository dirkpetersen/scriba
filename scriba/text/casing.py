"""Casing & spacing state machine (DESIGN.md §7.5 point 5).

Carries `PostprocState` across utterances: if the previous injected text ended
a sentence (`.?!`), the next utterance is capitalized and a space is prepended;
if it ended with `\\n`, the next utterance is capitalized with no leading
space; otherwise the next utterance is lowercased with a leading space. The
state resets to the initial values (capitalize, no leading space) whenever the
foreground window's hwnd differs from the hwnd recorded in `state` (typing
continued into a different app makes no sense mid-"sentence").

Pure module: no I/O, no globals. `apply_casing` never mutates its `state`
argument -- it always returns a new `PostprocState` for the caller to carry
into the next utterance.
"""

from ..messages import ForegroundWindow, PostprocState

_SENTENCE_END_CHARS = (".", "?", "!")


def apply_casing(
    text: str, state: PostprocState, foreground: ForegroundWindow | None
) -> tuple[str, PostprocState]:
    """Apply casing/spacing to `text` per `state`, return `(text, next_state)`.

    `state.last_hwnd` is compared against `foreground`'s hwnd (`None` if there
    is no foreground window); a mismatch resets to the initial state before
    casing is applied. The returned state always has `last_hwnd` set to the
    current foreground's hwnd.
    """
    hwnd = foreground.hwnd if foreground is not None else None
    if hwnd != state.last_hwnd:
        state = PostprocState(last_hwnd=hwnd)

    if not text:
        return text, PostprocState(
            capitalize_next=state.capitalize_next,
            space_before_next=state.space_before_next,
            last_hwnd=hwnd,
        )

    if state.capitalize_next:
        text = text[0].upper() + text[1:]
    else:
        text = text[0].lower() + text[1:]
    if state.space_before_next:
        text = " " + text

    return text, _next_state(text, hwnd)


def _next_state(text: str, hwnd: int | None) -> PostprocState:
    if text.endswith(_SENTENCE_END_CHARS):
        return PostprocState(capitalize_next=True, space_before_next=True, last_hwnd=hwnd)
    if text.endswith("\n"):
        return PostprocState(capitalize_next=True, space_before_next=False, last_hwnd=hwnd)
    return PostprocState(capitalize_next=False, space_before_next=True, last_hwnd=hwnd)
