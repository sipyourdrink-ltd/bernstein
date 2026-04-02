"""Usage budget tracking and overage detection for Bernstein.

Reads daily/weekly token and cost budgets from ``.bernstein/usage_budget.yaml``,
aggregates today's consumption from ``.sdd/metrics/`` JSONL records, and
provides Rich-formatted progress reports.

When usage exceeds the configured limit, ``is_over_budget()`` returns ``True``
so callers can throttle or stop spawning agents.

Config schema (``.bernstein/usage_budget.yaml``)::

    daily_limit_usd: 10.0
    daily_limit_tokens: 100000
    timezone: UTC          # optional, defaults to UTC

"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional yaml import — graceful fallback if PyYAML is unavailable.
# ---------------------------------------------------------------------------

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageBudget:
    """Holds both limits and current consumption totals.

    Attributes:
        daily_limit_usd: Maximum USD spend allowed per day (``None`` = unlimited).
        daily_limit_tokens: Maximum token consumption per day (``None`` = unlimited).
        current_usage_usd: USD consumed so far today.
        current_usage_tokens: Tokens consumed so far today.
        reset_at_ts: Unix timestamp when the daily budget resets (next midnight).
        config_path: Path to the config file that was loaded (for diagnostics).
    """

    daily_limit_usd: float | None = None
    daily_limit_tokens: int | None = None
    current_usage_usd: float = 0.0
    current_usage_tokens: int = 0
    reset_at_ts: float = 0.0
    config_path: str = ""


@dataclass(frozen=True)
class UsageBudgetConfig:
    """Parsed usage budget configuration from YAML.

    Attributes:
        daily_limit_usd: Maximum USD spend allowed per day (``None`` = unlimited).
        daily_limit_tokens: Maximum token consumption per day (``None`` = unlimited).
        timezone: Timezone for daily reset (currently only UTC is supported).
    """

    daily_limit_usd: float | None = None
    daily_limit_tokens: int | None = None
    timezone: str = "UTC"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_usage_budget_config(workdir: Path) -> UsageBudgetConfig | None:
    """Load ``.bernstein/usage_budget.yaml`` from *workdir*.

    Returns ``None`` if the file does not exist or yaml is unavailable.
    Logs a warning and returns ``None`` if the file is malformed.

    Args:
        workdir: Project root containing the ``.bernstein/`` directory.

    Returns:
        Usable config or ``None`` (feature disabled).
    """
    if _yaml is None:
        logger.debug(
            "PyYAML not installed — usage budget tracking disabled. Install with: pip install pyyaml",
        )
        return None

    config_path = workdir / ".bernstein" / "usage_budget.yaml"
    if not config_path.exists():
        logger.debug("No usage budget config at %s — feature disabled", config_path)
        return None

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = _yaml.safe_load(fh) or {}
    except Exception:
        logger.exception("Failed to read %s — usage budget feature disabled", config_path)
        return None

    daily_limit_usd: float | None = raw.get("daily_limit_usd")
    daily_limit_tokens: int | None = raw.get("daily_limit_tokens")
    # Coerce from JSONL numbers that might be float for the limit_tokens field.
    if daily_limit_tokens is not None:
        daily_limit_tokens = int(daily_limit_tokens)

    timezone: str = raw.get("timezone", "UTC") or "UTC"

    return UsageBudgetConfig(
        daily_limit_usd=daily_limit_usd,
        daily_limit_tokens=daily_limit_tokens,
        timezone=timezone,
    )


# ---------------------------------------------------------------------------
# Metrics reading
# ---------------------------------------------------------------------------


def _today_prefix() -> str:
    """Return today's date string for metric file naming.

    Returns:
        Date in ``YYYY-MM-DD`` format (UTC).
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _next_midnight_ts() -> float:
    """Compute the Unix timestamp for the next midnight UTC.

    Returns:
        Seconds since epoch for the next UTC midnight.
    """
    now = datetime.now(UTC)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Add one day via timedelta to avoid DST issues

    tomorrow += timedelta(days=1)
    return tomorrow.timestamp()


def _read_cost_metric_points(metrics_dir: Path, today: str) -> list[dict[str, Any]]:
    """Read all ``cost_efficiency_{today}.jsonl`` points from *metrics_dir*.

    This is the most reliably-written cost metric — it covers every completed
    task that had a cost > 0.

    Args:
        metrics_dir: Path to ``.sdd/metrics/`` directory.
        today: Date string ``YYYY-MM-DD``.

    Returns:
        List of dicts with ``value`` (float USD) per record.
    """
    filepath = metrics_dir / f"cost_efficiency_{today}.jsonl"
    return _read_jsonl(filepath)


def _read_api_usage_metric_points(metrics_dir: Path, today: str) -> list[dict[str, Any]]:
    """Read all ``api_usage_{today}.jsonl`` points from *metrics_dir*.

    Each record's ``value`` field is the token count for one task.

    Args:
        metrics_dir: Path to ``.sdd/metrics/`` directory.
        today: Date string ``YYYY-MM-DD``.

    Returns:
        List of dicts with ``value`` (float tokens) per record.
    """
    filepath = metrics_dir / f"api_usage_{today}.jsonl"
    return _read_jsonl(filepath)


def _read_jsonl(filepath: Path) -> list[dict[str, Any]]:
    """Read a JSONL file and return a list of parsed dicts.

    Missing or unreadable files return an empty list (logged).

    Args:
        filepath: Path to the JSONL file.

    Returns:
        Parsed records, one per line.
    """
    if not filepath.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with filepath.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError:
                    logger.warning("Malformed JSONL line in %s: %s", filepath, stripped[:80])
    except OSError:
        logger.exception("Failed to read metrics from %s", filepath)
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_usage_budget(workdir: Path) -> UsageBudget:
    """Read current usage from metrics JSONL and project config.

    Loads the budget limits from ``.bernstein/usage_budget.yaml`` and sums
    today's consumption from ``.sdd/metrics/`` cost and API usage JSONL
    files.

    Args:
        workdir: Project root containing ``.bernstein/`` and ``.sdd/``.

    Returns:
        Aggregated ``UsageBudget`` with limits and current usage.
    """
    config = load_usage_budget_config(workdir)
    config_path = str(workdir / ".bernstein" / "usage_budget.yaml" if config else "")

    sdd_metrics_dir = workdir / ".sdd" / "metrics"
    today = _today_prefix()

    # Aggregate cost
    cost_points = _read_cost_metric_points(sdd_metrics_dir, today)
    current_usage_usd = sum(float(p.get("value", 0.0)) for p in cost_points)

    # Aggregate tokens
    api_points = _read_api_usage_metric_points(sdd_metrics_dir, today)
    current_usage_tokens = int(sum(float(p.get("value", 0)) for p in api_points))

    return UsageBudget(
        daily_limit_usd=config.daily_limit_usd if config else None,
        daily_limit_tokens=config.daily_limit_tokens if config else None,
        current_usage_usd=round(current_usage_usd, 4),
        current_usage_tokens=current_usage_tokens,
        reset_at_ts=_next_midnight_ts(),
        config_path=config_path,
    )


def is_over_budget(budget: UsageBudget) -> bool:
    """Check whether current usage exceeds configured limits.

    Returns ``False`` if no limits are configured (``None`` means unlimited).
    Returns ``True`` if *either* limit (USD or tokens) is exceeded.

    Args:
        budget: Current budget state from ``check_usage_budget()``.

    Returns:
        ``True`` when a configured cap is breached, ``False`` otherwise.
    """
    if budget.daily_limit_usd is not None and budget.current_usage_usd >= budget.daily_limit_usd:
        return True
    return budget.daily_limit_tokens is not None and budget.current_usage_tokens >= budget.daily_limit_tokens


def format_usage_report(budget: UsageBudget) -> str:
    """Produce a human-readable usage report string.

    Uses Rich markup (color, progress bars) when rendered on a TTY.
    Degrades gracefully to plain text otherwise.

    Args:
        budget: Current budget state to report on.

    Returns:
        Multi-line string suitable for ``console.print()``.
    """
    lines: list[str] = []
    lines.append("[bold]Usage Budget[/bold]")

    # --- USD line ---
    if budget.daily_limit_usd is not None:
        usd_pct = min(budget.current_usage_usd / budget.daily_limit_usd, 1.0)
        bar = _make_progress_bar(usd_pct, width=20)
        color = _budget_color(usd_pct)
        lines.append(
            f"  USD: [{color}]{bar}[/{color}]"
            f" ${budget.current_usage_usd:.2f} / ${budget.daily_limit_usd:.2f}"
            f"  ({usd_pct:.0%})"
        )
    else:
        lines.append("  USD: [dim]unlimited[/dim]")

    # --- Tokens line ---
    if budget.daily_limit_tokens is not None:
        tok_pct = min(budget.current_usage_tokens / budget.daily_limit_tokens, 1.0)
        bar = _make_progress_bar(tok_pct, width=20)
        color = _budget_color(tok_pct)
        lines.append(
            f"  Tokens: [{color}]{bar}[/{color}]"
            f" {budget.current_usage_tokens:,} / {budget.daily_limit_tokens:,}"
            f"  ({tok_pct:.0%})"
        )
    else:
        lines.append("  Tokens: [dim]unlimited[/dim]")

    # --- Overall status ---
    if is_over_budget(budget):
        lines.append("")
        lines.append("  [bold red]OVER BUDGET — stop spawning agents until reset[/bold red]")
    else:
        lines.append("")
        lines.append("  [green]Within budget[/green]")

    if budget.reset_at_ts > 0:
        reset_dt = datetime.fromtimestamp(budget.reset_at_ts, tz=UTC)
        lines.append(f"  Resets at: {reset_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    if budget.config_path:
        lines.append(f"  Config: {budget.config_path}")

    return "\n".join(lines)


def _make_progress_bar(pct: float, width: int = 20) -> str:
    """Build a simple text progress bar.

    Args:
        pct: Fraction filled (0.0 to 1.0).
        width: Total bar width in characters.

    Returns:
        String of ``■`` and ``░`` characters.
    """
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)


def _budget_color(pct: float) -> str:
    """Return a Rich color tag for the given utilisation percentage.

    Args:
        pct: Fraction 0.0 to 1.0 (clamped).

    Returns:
        Rich-style color name: green, yellow, or red.
    """
    if pct >= 1.0:
        return "bold red"
    if pct >= 0.75:
        return "yellow"
    return "green"
