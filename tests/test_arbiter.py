"""MicArbiter tests (DESIGN.md §7.2): synthetic per-device probability/RMS
traces, no real audio or VAD model involved.
"""

from scriba.config import AudioConfig
from scriba.detect.arbiter import MicArbiter

THRESHOLD = 0.5
WINDOW_S = 0.2
FRAME_S = 512 / 16_000


def test_single_crossing_device_wins_and_stays_sticky():
    arbiter = MicArbiter(AudioConfig(), window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        winner = arbiter.offer("mic1", 0.8, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == "mic1"
    # stays sticky even for a competing device offered afterwards
    late_winner = arbiter.offer("mic2", 0.99, rms=10.0, t_monotonic=t, threshold=THRESHOLD)
    assert late_winner == "mic1"


def test_highest_mean_probability_wins():
    arbiter = MicArbiter(AudioConfig(), window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        winner = arbiter.offer("loud", 0.9, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        arbiter.offer("quiet", 0.6, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == "loud"


def test_tie_break_by_highest_rms():
    arbiter = MicArbiter(AudioConfig(), window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        winner = arbiter.offer("mic_a", 0.8, rms=0.5, t_monotonic=t, threshold=THRESHOLD)
        arbiter.offer("mic_b", 0.8, rms=0.9, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == "mic_b"


def test_device_priority_biases_a_near_tie():
    config = AudioConfig(device_priority=["mic_a"])
    arbiter = MicArbiter(config, window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        # mic_b scores slightly higher, but within the priority epsilon of mic_a
        winner = arbiter.offer("mic_a", 0.70, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        arbiter.offer("mic_b", 0.72, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == "mic_a"


def test_device_priority_does_not_override_a_clear_winner():
    config = AudioConfig(device_priority=["mic_a"])
    arbiter = MicArbiter(config, window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        # mic_b is well beyond the priority epsilon ahead of mic_a
        winner = arbiter.offer("mic_a", 0.55, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        arbiter.offer("mic_b", 0.95, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == "mic_b"


def test_non_crossing_device_never_wins():
    arbiter = MicArbiter(AudioConfig(), window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        winner = arbiter.offer("silent", 0.1, rms=5.0, t_monotonic=t, threshold=THRESHOLD)
        arbiter.offer("speaking", 0.55, rms=0.01, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == "speaking"


def test_reset_allows_a_different_winner_next_utterance():
    arbiter = MicArbiter(AudioConfig(), window_s=WINDOW_S)
    t = 0.0
    winner = None
    for _ in range(10):
        winner = arbiter.offer("mic1", 0.8, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S
    assert winner == "mic1"

    arbiter.reset()

    winner = None
    for _ in range(10):
        winner = arbiter.offer("mic2", 0.8, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S
    assert winner == "mic2"


def test_no_winner_while_window_pending():
    arbiter = MicArbiter(AudioConfig(), window_s=WINDOW_S)
    # only 2 frames (~64ms), well short of the 200ms arbitration window
    assert arbiter.offer("mic1", 0.8, rms=0.1, t_monotonic=0.0, threshold=THRESHOLD) is None
    assert arbiter.offer("mic1", 0.8, rms=0.1, t_monotonic=FRAME_S, threshold=THRESHOLD) is None


def test_device_priority_matches_hashed_device_ids():
    from scriba.messages import device_id_for_name

    # config carries device *names*; offer() receives hashed device_ids
    name = "Headset Microphone (Jabra)"
    config = AudioConfig(device_priority=[name])
    arbiter = MicArbiter(config, window_s=WINDOW_S)
    hashed = device_id_for_name(name)
    t = 0.0
    winner = None
    for _ in range(10):
        winner = arbiter.offer(hashed, 0.70, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        arbiter.offer("other_mic", 0.72, rms=0.1, t_monotonic=t, threshold=THRESHOLD)
        t += FRAME_S

    assert winner == hashed
