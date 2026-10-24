"""Spotting a model that has started repeating itself mid-stream.

Small models sometimes fall into a loop: the same line written over and
over (a reasoning stream once repeated "- Get-VMReplication." 97 times
until the user stopped the turn). The tool-call stall check cannot see
that, because no call is made. This watches the streamed text and the
reasoning as they arrive and says when the tail has become a loop: one line
filling most of the recent lines, or one stretch of text repeating back to
back many times.

Thresholds are high on purpose: a table, a list of similar items or a
separator line must never trip it.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

TAIL_CHARS = 6000
CHECK_EVERY = 300          # characters between checks
LINE_WINDOW = 50           # recent lines looked at
LINE_REPEATS = 25          # one line this many times among them is a loop
MIN_LINE_CHARS = 4
PERIOD_MIN = 8             # a repeating stretch at least this long...
PERIOD_MAX = 400
PERIOD_REPEATS = 12        # ...back to back this many times...
PERIOD_SPAN = 600          # ...covering at least this much text


class RepetitionGuard:
    """Feed it the stream; `loop` is set (with a sample) once it has looped."""

    def __init__(self) -> None:
        self.tail = ""
        self.since_check = 0
        self.loop: Optional[str] = None

    def feed(self, delta: str) -> Optional[str]:
        if self.loop or not delta:
            return self.loop
        self.tail = (self.tail + delta)[-TAIL_CHARS:]
        self.since_check += len(delta)
        if self.since_check >= CHECK_EVERY:
            self.since_check = 0
            self.loop = repeating(self.tail)
        return self.loop


def repeating(text: str) -> Optional[str]:
    """A sample of what repeats when `text` ends in a loop, else None."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    recent = lines[-LINE_WINDOW:]
    if len(recent) >= LINE_REPEATS:
        line, count = Counter(recent).most_common(1)[0]
        if count >= LINE_REPEATS and len(line) >= MIN_LINE_CHARS:
            return line[:120]
    tail = text[-(PERIOD_MAX * PERIOD_REPEATS + PERIOD_MAX):]
    for period in range(PERIOD_MIN, PERIOD_MAX + 1):
        if period * PERIOD_REPEATS > len(tail):
            break
        unit = tail[-period:]
        if not unit.strip():
            continue
        repeats = 1
        position = len(tail) - period
        while position - period >= 0 and tail[position - period:position] == unit:
            repeats += 1
            position -= period
        if repeats >= PERIOD_REPEATS and repeats * period >= PERIOD_SPAN:
            # A run of one character (a separator line) is decoration, not a loop.
            if len(set(unit.strip())) <= 2:
                continue
            return " ".join(unit.split())[:120]
    return None
