"""TUI-006: Cost sparkline for the TUI sidebar.

Tracks cost accumulation over time and renders a sparkline showing
the spend trajectory. The sparkline updates in real-time as cost
data arrives from the task server.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from rich.text import Text

from bernstein.tui.widgets import SPARKLINE_CHARS


@dataclass
class CostSample:
    """A single cost measurement at a point in time.

    Attributes:
        timestamp: Unix timestamp when the sample was taken.
        cumulative_usd: Total cumulative cost in USD at this point.
    """

    timestamp: float
    cumulative_usd: float


@dataclass
class CostTracker:
    """Tracks cost samples over time for sparkline rendering.

    Maintains a ring buffer of cost samples and provides methods for
    computing deltas (spend rate per interval) suitable for sparkline
    display.

    Attributes:
        max_samples: Maximum number of samples to retain.
        samples: Ring buffer of cost samples.
    """

    max_samples: int = 120
    samples: deque[CostSample] = field(default_factory=lambda: deque(maxlen=120))

    def __post_init__(self) -> None:
        """Re-create the deque with the correct maxlen if needed."""
        if self.samples.maxlen != self.max_samples:
            self.samples = deque(self.samples, maxlen=self.max_samples)

    def add_sample(self, cumulative_usd: float, timestamp: float | None = None) -> None:
        """Record a new cost sample.

        Args:
            cumulative_usd: Total cumulative cost in USD.
            timestamp: Sample timestamp (defaults to now).
        """
        if timestamp is None:
            timestamp = time.time()
        self.samples.append(CostSample(timestamp=timestamp, cumulative_usd=cumulative_usd))

    def delta_series(self) -> list[float]:
        """Compute per-interval cost deltas from the sample buffer.

        Returns:
            List of cost deltas between consecutive samples.
            Empty if fewer than 2 samples exist.
        """
        if len(self.samples) < 2:
            return []
        result: list[float] = []
        samples_list = list(self.samples)
        for i in range(1, len(samples_list)):
            delta = samples_list[i].cumulative_usd - samples_list[i - 1].cumulative_usd
            result.append(max(0.0, delta))
        return result

    def cumulative_series(self) -> list[float]:
        """Return the raw cumulative cost series.

        Returns:
            List of cumulative USD values in time order.
        """
        return [s.cumulative_usd for s in self.samples]

    @property
    def latest_cost(self) -> float:
        """Return the most recent cumulative cost, or 0.0 if no samples."""
        if self.samples:
            return self.samples[-1].cumulative_usd
        return 0.0

    @property
    def total_spend_rate(self) -> float:
        """Compute the average spend rate in USD/minute.

        Returns:
            Average spend rate, or 0.0 if insufficient data.
        """
        if len(self.samples) < 2:
            return 0.0
        first = self.samples[0]
        last = self.samples[-1]
        elapsed_min = (last.timestamp - first.timestamp) / 60.0
        if elapsed_min <= 0:
            return 0.0
        return (last.cumulative_usd - first.cumulative_usd) / elapsed_min


#: Width thresholds for sparkline rendering (chars).
_DEFAULT_SPARKLINE_WIDTH: int = 12
_COMPACT_SPARKLINE_WIDTH: int = 8


def render_cost_sparkline(
    deltas: list[float],
    *,
    width: int = _DEFAULT_SPARKLINE_WIDTH,
) -> str:
    """Render a cost-delta sparkline as a plain string.

    Args:
        deltas: Per-interval cost deltas.
        width: Maximum number of sparkline characters.

    Returns:
        Sparkline string using block characters. Empty string if no data.
    """
    if not deltas:
        return ""
    recent = deltas[-width:] if len(deltas) > width else deltas
    max_val = max(recent)
    if max_val <= 0:
        return SPARKLINE_CHARS[0] * len(recent)
    chars: list[str] = []
    for val in recent:
        normalized = val / max_val
        idx = int(normalized * (len(SPARKLINE_CHARS) - 1))
        chars.append(SPARKLINE_CHARS[idx])
    return "".join(chars)


def render_cost_sparkline_rich(
    tracker: CostTracker,
    *,
    width: int = _DEFAULT_SPARKLINE_WIDTH,
    show_rate: bool = True,
) -> Text:
    """Render a Rich Text sparkline with cost metadata.

    Args:
        tracker: CostTracker with accumulated samples.
        width: Sparkline character width.
        show_rate: Whether to append spend rate.

    Returns:
        Rich Text object with colored sparkline and optional rate.
    """
    text = Text()
    deltas = tracker.delta_series()
    sparkline = render_cost_sparkline(deltas, width=width)

    cost = tracker.latest_cost
    if cost <= 0 and not sparkline:
        text.append("$0.00", style="dim")
        return text

    # Color based on spend rate
    rate = tracker.total_spend_rate
    if rate > 1.0:
        color = "red"
    elif rate > 0.3:
        color = "yellow"
    else:
        color = "green"

    text.append(f"${cost:.2f} ", style="bold")
    if sparkline:
        text.append(sparkline, style=color)
    if show_rate and rate > 0:
        text.append(f" ${rate:.2f}/min", style="dim")
    return text


def render_cost_sidebar(
    tracker: CostTracker,
    *,
    budget_usd: float | None = None,
    width: int = _DEFAULT_SPARKLINE_WIDTH,
) -> Text:
    """Render a complete cost sidebar section with budget tracking.

    Args:
        tracker: CostTracker with accumulated samples.
        budget_usd: Optional budget cap in USD.
        width: Sparkline character width.

    Returns:
        Multi-line Rich Text for sidebar display.
    """
    text = Text()
    text.append("Cost ", style="bold dim")
    sparkline_text = render_cost_sparkline_rich(tracker, width=width)
    text.append_text(sparkline_text)

    if budget_usd is not None and budget_usd > 0:
        cost = tracker.latest_cost
        pct = min(cost / budget_usd, 1.0)
        remaining = max(0.0, budget_usd - cost)
        if pct >= 0.9:
            budget_color = "red"
        elif pct >= 0.7:
            budget_color = "yellow"
        else:
            budget_color = "green"
        text.append(
            f"\n  Budget: [{budget_color}]${cost:.2f}/${budget_usd:.2f}[/{budget_color}] (${remaining:.2f} remaining)"
        )
    return text
