"""Decisions for the WDBS volume meter (no Qt, no I/O). The painting and pactl plumbing live in wdbs.volume.

The meter shows when the system volume or mute state CHANGES, whoever changed it (phone remote, air-mouse, keyboard).
The first reading after start is only a baseline, and PulseAudio's sink events for plain state changes (idle <->
running when audio starts) never reach the screen because nothing about the reading differs.
"""

from __future__ import annotations

import re

SEGMENTS = 20
LINGER_S = 2.5          # fully visible this long after the last change
FADE_S = 0.3
SLIDE_S = 0.15

Reading = tuple[int, bool]      # (percent, muted)


def parse_reading(volume_text: str, mute_text: str) -> Reading | None:
    """`pactl get-sink-volume` + `pactl get-sink-mute` output -> (percent, muted); None if unreadable.

    The loudest channel wins, so a balance tweak still reads as the level the listener hears.
    """
    pcts = [int(m) for m in re.findall(r"(\d+)%", volume_text or "")]
    if not pcts:
        return None
    return max(pcts), bool(re.search(r"mute:\s*yes", mute_text or "", re.I))


def lit_segments(percent: int) -> int:
    """How many of the SEGMENTS blocks light for `percent` (clamped to 0..100; over-amplified reads as full)."""
    return round(max(0, min(100, percent)) / 100 * SEGMENTS)


def is_sink_event(line: str) -> bool:
    """A `pactl subscribe` line that may mean the default sink's volume or mute changed."""
    return bool(re.match(r"Event '(change|new)' on (sink|server)(?![\w-])", line or ""))


class Meter:
    """When the meter is up and how far along its slide-in and fade-out it is."""

    def __init__(self) -> None:
        self.reading: Reading | None = None
        self.shown_at: float | None = None

    def observe(self, reading: Reading | None, now: float) -> bool:
        """Feed a new reading. True when it changed and the meter should (re)appear."""
        if reading is None:
            return False
        if self.reading is None:        # baseline: what the volume already was is not news
            self.reading = reading
            return False
        if reading == self.reading:
            return False
        self.reading, self.shown_at = reading, now
        return True

    def state(self, now: float) -> tuple[float, float]:
        """(alpha, slide) in 0..1. slide 0 = just off its edge, 1 = in place. (0, 0) when idle."""
        if self.shown_at is None:
            return 0.0, 0.0
        t = now - self.shown_at
        if t >= LINGER_S + FADE_S:
            self.shown_at = None
            return 0.0, 0.0
        slide = min(1.0, t / SLIDE_S)
        slide = 1 - (1 - slide) ** 2    # ease-out
        alpha = 1.0 if t <= LINGER_S else 1.0 - (t - LINGER_S) / FADE_S
        return alpha, slide

    @property
    def visible(self) -> bool:
        return self.shown_at is not None
