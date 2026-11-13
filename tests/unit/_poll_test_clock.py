"""Virtual clock shared by the CLI status-poll tests."""

from __future__ import annotations


class PollClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, "the polling interval must advance the clock"
        self.sleeps.append(seconds)
        self.now += seconds
