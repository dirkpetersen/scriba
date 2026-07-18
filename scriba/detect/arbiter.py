"""Multi-mic arbitration (DESIGN.md §7.2).

Multiple enabled microphones all hear the same speech, but exactly one
stream may feed an utterance -- otherwise the user gets double text. Every
enabled device runs its own `SileroVad` independently; this module decides,
once one or more of them cross the speech threshold, which single device
gets to drive the `UtteranceSegmenter` for that utterance.
"""

from dataclasses import dataclass, field

from ..config import AudioConfig
from ..messages import device_id_for_name

_ARBITRATION_WINDOW_S = 0.2  # DESIGN §7.2: "arbitration window (~200 ms)"

# How close two devices' mean VAD probability must be (absolute difference)
# for `device_priority` to be allowed to override the higher-scoring one.
# Keeps priority a genuine *bias* rather than an outright veto of a clearly
# better mic.
_PRIORITY_EPSILON = 0.05


@dataclass
class _Candidate:
    probs: list[float] = field(default_factory=list)
    rms: list[float] = field(default_factory=list)
    qualified: bool = False  # crossed the threshold at least once this window

    def mean_prob(self) -> float:
        return sum(self.probs) / len(self.probs)

    def mean_rms(self) -> float:
        return sum(self.rms) / len(self.rms)


class MicArbiter:
    """Picks, and sticks to, one winning device per utterance.

    Feed *every* enabled device's *every* frame through `offer()`, whether or
    not it turns out to be the winner -- devices are only compared while no
    winner is locked yet. Once one or more devices cross `threshold` within
    the arbitration window, the winner is the one with the highest mean VAD
    probability over that window (ties broken by highest mean RMS), optionally
    biased by `audio_config.device_priority`. The winner then stays sticky
    (every `offer()` call returns it immediately, cheaply) until `reset()` is
    called -- the detector orchestrator calls `reset()` once the winning
    device's utterance has fully closed, so a different mic can win next time.
    """

    def __init__(self, audio_config: AudioConfig, window_s: float = _ARBITRATION_WINDOW_S):
        # config lists device *names*, but offer() receives hashed device_ids
        # (messages.device_id_for_name) -- key priority by both so either matches.
        self._priority: dict[str, int] = {}
        for rank, name in enumerate(audio_config.device_priority):
            self._priority.setdefault(name, rank)
            self._priority.setdefault(device_id_for_name(name), rank)
        self._window_s = window_s
        self._winner: str | None = None
        self._window_start: float | None = None
        self._candidates: dict[str, _Candidate] = {}

    def reset(self) -> None:
        """Unlocks the sticky winner and discards any in-progress arbitration window."""
        self._winner = None
        self._window_start = None
        self._candidates = {}

    def offer(
        self, device_id: str, prob: float, rms: float, t_monotonic: float, threshold: float
    ) -> str | None:
        """Records one device's one frame; returns the winning device_id once decided.

        Returns None while arbitration is still pending (no device has
        crossed threshold yet, or the ~200ms window hasn't elapsed).
        """
        if self._winner is not None:
            return self._winner

        crossed = prob >= threshold
        if crossed and self._window_start is None:
            self._window_start = t_monotonic

        if self._window_start is not None:
            candidate = self._candidates.setdefault(device_id, _Candidate())
            candidate.probs.append(prob)
            candidate.rms.append(rms)
            if crossed:
                candidate.qualified = True

        if self._window_start is None:
            return None
        if t_monotonic - self._window_start < self._window_s:
            return None

        self._winner = self._resolve()
        self._window_start = None
        self._candidates = {}
        return self._winner

    def _resolve(self) -> str | None:
        qualified = {d: c for d, c in self._candidates.items() if c.qualified}
        if not qualified:
            return None

        scored = {device_id: c.mean_prob() for device_id, c in qualified.items()}
        best_prob = max(scored.values())
        near_best = [d for d, p in scored.items() if p >= best_prob - _PRIORITY_EPSILON]

        if self._priority:
            prioritized = [d for d in near_best if d in self._priority]
            if prioritized:
                return min(prioritized, key=lambda d: self._priority[d])

        return max(near_best, key=lambda d: (scored[d], qualified[d].mean_rms()))
