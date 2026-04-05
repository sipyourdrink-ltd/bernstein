"""Prompt token usage analyzer.

Reads task metrics from ``.sdd/metrics/tasks.jsonl``, computes per-task
token distribution stats, identifies waste patterns, and produces a
markdown report with actionable suggestions.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.cost import MODEL_COSTS_PER_1M_TOKENS

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HIGH_RATIO_THRESHOLD: float = 10.0
"""Input:output ratios above this are flagged as probable prompt bloat."""

EFFICIENCY_RATIO_THRESHOLD: float = 3.0
"""Ideal ceiling for input:output ratio."""

MINIMAL_OUTPUT_THRESHOLD: int = 100
"""Tasks producing fewer output tokens than this are flagged."""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TaskTokenStats:
    """Per-task token breakdown."""

    task_id: str
    title: str
    model: str
    tokens_prompt: int
    tokens_completion: int
    cost_usd: float
    io_ratio: float
    """Input / output token ratio (inf when output is 0)."""


@dataclass
class WastePattern:
    """A detected token waste pattern."""

    task_id: str
    title: str
    pattern: str
    detail: str


@dataclass
class ModelSpend:
    """Aggregate spend for a single model."""

    model: str
    total_tokens_prompt: int
    total_tokens_completion: int
    total_cost_usd: float
    task_count: int


@dataclass
class TokenAnalysis:
    """Complete token analysis result."""

    task_stats: list[TaskTokenStats] = field(default_factory=list[TaskTokenStats])
    waste_patterns: list[WastePattern] = field(default_factory=list[WastePattern])
    model_spend: list[ModelSpend] = field(default_factory=list[ModelSpend])
    total_tokens_prompt: int = 0
    total_tokens_completion: int = 0
    total_cost_usd: float = 0.0
    overall_io_ratio: float = 0.0
    top_5_hungry: list[TaskTokenStats] = field(default_factory=list[TaskTokenStats])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cost_for_model(model: str, tokens_prompt: int, tokens_completion: int) -> float:
    """Compute cost in USD using per-model pricing.

    Falls back to 0 when the model is not in the pricing table.
    """
    model_lower = model.lower()
    for key, prices in MODEL_COSTS_PER_1M_TOKENS.items():
        if key in model_lower:
            input_price = prices.get("input", 0.0) or 0.0
            output_price = prices.get("output", 0.0) or 0.0
            return (tokens_prompt / 1_000_000 * input_price) + (tokens_completion / 1_000_000 * output_price)
    return 0.0


def _io_ratio(tokens_prompt: int, tokens_completion: int) -> float:
    """Return input/output token ratio, capping at 999.0 when output is 0."""
    if tokens_completion <= 0:
        return 999.0
    return tokens_prompt / tokens_completion


def _load_tasks_jsonl(metrics_dir: Path) -> list[dict[str, Any]]:
    """Load task metric records from tasks.jsonl."""
    p = metrics_dir / "tasks.jsonl"
    if not p.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class TokenUsageAnalyzer:
    """Analyzes prompt token distribution and suggests reductions.

    Args:
        workdir: Project root (parent of ``.sdd/``).
    """

    def __init__(self, workdir: Path) -> None:
        self._metrics_dir = workdir / ".sdd" / "metrics"

    def analyze(self, records: list[dict[str, Any]] | None = None) -> TokenAnalysis:
        """Run the full analysis.

        Args:
            records: Optional pre-loaded task records. When *None*, records
                are loaded from ``tasks.jsonl`` on disk.

        Returns:
            A populated ``TokenAnalysis`` dataclass.
        """
        if records is None:
            records = _load_tasks_jsonl(self._metrics_dir)

        # De-duplicate by task_id, keeping last record per task.
        seen: dict[str, dict[str, Any]] = {}
        for rec in records:
            tid = rec.get("task_id", "")
            if tid:
                seen[tid] = rec

        # Build per-task stats.
        task_stats: list[TaskTokenStats] = []
        title_counts: dict[str, list[str]] = {}
        for tid, rec in seen.items():
            tprompt = int(rec.get("tokens_prompt", 0) or 0)
            tcompletion = int(rec.get("tokens_completion", 0) or 0)
            model = str(rec.get("model", "unknown") or "unknown")
            cost = float(rec.get("cost_usd", 0.0) or 0.0)
            if cost == 0.0 and (tprompt > 0 or tcompletion > 0):
                cost = _cost_for_model(model, tprompt, tcompletion)
            title = str(rec.get("title", tid) or tid)
            ratio = _io_ratio(tprompt, tcompletion)

            task_stats.append(
                TaskTokenStats(
                    task_id=tid,
                    title=title,
                    model=model,
                    tokens_prompt=tprompt,
                    tokens_completion=tcompletion,
                    cost_usd=cost,
                    io_ratio=ratio,
                )
            )

            # Track titles for duplicate detection.
            norm_title = title.strip().lower()
            if norm_title:
                title_counts.setdefault(norm_title, []).append(tid)

        # Waste patterns.
        waste: list[WastePattern] = []
        for ts in task_stats:
            if ts.io_ratio >= HIGH_RATIO_THRESHOLD:
                waste.append(
                    WastePattern(
                        task_id=ts.task_id,
                        title=ts.title,
                        pattern="high_io_ratio",
                        detail=(
                            f"Input:output ratio {ts.io_ratio:.1f}:1 "
                            f"({ts.tokens_prompt:,} in / {ts.tokens_completion:,} out) "
                            f"— consider reducing context"
                        ),
                    )
                )
            if 0 < ts.tokens_completion < MINIMAL_OUTPUT_THRESHOLD:
                waste.append(
                    WastePattern(
                        task_id=ts.task_id,
                        title=ts.title,
                        pattern="minimal_output",
                        detail=(
                            f"Only {ts.tokens_completion} output tokens for "
                            f"{ts.tokens_prompt:,} input — task may have failed silently"
                        ),
                    )
                )

        # Repeated task retries (same title, multiple IDs).
        for norm_title, tids in title_counts.items():
            if len(tids) > 1:
                waste.append(
                    WastePattern(
                        task_id=tids[0],
                        title=norm_title,
                        pattern="repeated_retry",
                        detail=f"{len(tids)} attempts with same title — wasted tokens on retries",
                    )
                )

        # Model spend aggregation.
        model_agg: dict[str, ModelSpend] = {}
        for ts in task_stats:
            ms = model_agg.get(ts.model)
            if ms is None:
                ms = ModelSpend(
                    model=ts.model,
                    total_tokens_prompt=0,
                    total_tokens_completion=0,
                    total_cost_usd=0.0,
                    task_count=0,
                )
                model_agg[ts.model] = ms
            ms.total_tokens_prompt += ts.tokens_prompt
            ms.total_tokens_completion += ts.tokens_completion
            ms.total_cost_usd += ts.cost_usd
            ms.task_count += 1

        model_spend = sorted(model_agg.values(), key=lambda m: -m.total_cost_usd)

        # Totals.
        total_prompt = sum(ts.tokens_prompt for ts in task_stats)
        total_completion = sum(ts.tokens_completion for ts in task_stats)
        total_cost = sum(ts.cost_usd for ts in task_stats)
        overall_ratio = _io_ratio(total_prompt, total_completion)

        # Top 5 most token-hungry tasks (by total tokens).
        top5 = sorted(
            task_stats,
            key=lambda t: t.tokens_prompt + t.tokens_completion,
            reverse=True,
        )[:5]

        return TokenAnalysis(
            task_stats=task_stats,
            waste_patterns=waste,
            model_spend=model_spend,
            total_tokens_prompt=total_prompt,
            total_tokens_completion=total_completion,
            total_cost_usd=total_cost,
            overall_io_ratio=overall_ratio,
            top_5_hungry=top5,
        )


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def to_markdown(analysis: TokenAnalysis) -> str:
    """Render a ``TokenAnalysis`` as a readable markdown report.

    Args:
        analysis: The analysis result from ``TokenUsageAnalyzer.analyze()``.

    Returns:
        Multi-line markdown string.
    """
    lines: list[str] = []

    lines.append("# Token Usage Report")
    lines.append("")

    # Summary.
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total input tokens:** {analysis.total_tokens_prompt:,}")
    lines.append(f"- **Total output tokens:** {analysis.total_tokens_completion:,}")
    lines.append(f"- **Overall input:output ratio:** {analysis.overall_io_ratio:.1f}:1")
    eff = "good" if analysis.overall_io_ratio <= EFFICIENCY_RATIO_THRESHOLD else "high"
    lines.append(f"- **Efficiency:** {eff} (target < {EFFICIENCY_RATIO_THRESHOLD:.0f}:1)")
    lines.append(f"- **Total cost:** ${analysis.total_cost_usd:.4f}")
    lines.append("")

    # Spend by model.
    if analysis.model_spend:
        lines.append("## Spend by Model")
        lines.append("")
        lines.append("| Model | Tasks | Input Tokens | Output Tokens | Cost USD |")
        lines.append("|-------|------:|-------------:|--------------:|---------:|")
        for ms in analysis.model_spend:
            lines.append(
                f"| {ms.model} | {ms.task_count} "
                f"| {ms.total_tokens_prompt:,} "
                f"| {ms.total_tokens_completion:,} "
                f"| ${ms.total_cost_usd:.4f} |"
            )
        lines.append("")

    # Top 5 most token-hungry tasks.
    if analysis.top_5_hungry:
        lines.append("## Top 5 Most Token-Hungry Tasks")
        lines.append("")
        lines.append("| Task | Model | Input | Output | Ratio | Cost |")
        lines.append("|------|-------|------:|-------:|------:|-----:|")
        for ts in analysis.top_5_hungry:
            short_title = ts.title[:50] + ("..." if len(ts.title) > 50 else "")
            lines.append(
                f"| {short_title} | {ts.model} "
                f"| {ts.tokens_prompt:,} "
                f"| {ts.tokens_completion:,} "
                f"| {ts.io_ratio:.1f}:1 "
                f"| ${ts.cost_usd:.4f} |"
            )
        lines.append("")

    # Waste patterns / suggestions.
    if analysis.waste_patterns:
        lines.append("## Suggestions")
        lines.append("")
        for wp in analysis.waste_patterns:
            short_title = wp.title[:60] + ("..." if len(wp.title) > 60 else "")
            lines.append(f"- **{short_title}** ({wp.task_id}): {wp.detail}")
        lines.append("")

    if not analysis.waste_patterns:
        lines.append("## Suggestions")
        lines.append("")
        lines.append("No waste patterns detected.")
        lines.append("")

    return "\n".join(lines)
