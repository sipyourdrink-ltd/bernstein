"""Prompt token-usage breakdown: system prompt %, context %, user prompt %.

After a prompt is assembled from named sections in :mod:`spawn_prompt`, this
module categorises each section into one of three buckets:

- **system_prompt** — the role template / persona instructions
- **context** — project state, lessons, rich context, predecessors
- **user_prompt** — tasks, instructions, protocol boilerplate (git safety,
  heartbeat, signals, coordination, nudges, file ownership)

It estimates the token count of each section, computes per-category totals and
percentages, and optionally writes a JSON report to
``.sdd/metrics/prompt_token_usage_{session_id}.json``.

The analysis also produces actionable reduction suggestions: if any category
exceeds its recommended budget the suggestion names the largest sections to
trim.

Usage::

    from bernstein.core.prompt_token_analysis import analyse_prompt_sections

    report = analyse_prompt_sections(named_sections, session_id="abc")
    print(report.summary())
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.token_estimation import estimate_tokens_for_text

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section → category mapping
# ---------------------------------------------------------------------------

#: Sections that belong to the "system_prompt" category.
_SYSTEM_PROMPT_SECTIONS: frozenset[str] = frozenset(
    ["role", "specialists"]
)

#: Sections that belong to the "context" category.
_CONTEXT_SECTIONS: frozenset[str] = frozenset(
    ["context", "project", "lessons", "predecessor", "rich_context", "recommendations"]
)

# Everything else falls into "user_prompt" (tasks, instructions, protocol
# boilerplate: git_safety, heartbeat, signal, team coordination, team
# awareness, meta nudges, file ownership, …).

_CATEGORY_LABELS: dict[str, str] = {
    "system_prompt": "System prompt (role/persona)",
    "context": "Context (project state, lessons, predecessors)",
    "user_prompt": "User prompt (tasks, instructions, protocol)",
}

#: Recommended upper-bound token percentages per category.
_RECOMMENDED_PCT: dict[str, float] = {
    "system_prompt": 20.0,
    "context": 50.0,
    "user_prompt": 40.0,
}


def _section_category(section_name: str) -> str:
    """Return the category key for a named prompt section.

    Args:
        section_name: Name as used in the ``named_sections`` list.

    Returns:
        One of ``"system_prompt"``, ``"context"``, or ``"user_prompt"``.
    """
    name = section_name.lower()
    if name in _SYSTEM_PROMPT_SECTIONS:
        return "system_prompt"
    if name in _CONTEXT_SECTIONS:
        return "context"
    return "user_prompt"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SectionTokens:
    """Token estimate for a single named prompt section.

    Attributes:
        name: Section name (e.g. ``"role"``, ``"lessons"``).
        category: Bucket this section belongs to.
        tokens: Estimated token count.
        chars: Raw character count.
        pct_of_total: Percentage of the full prompt token count.
    """

    name: str
    category: str
    tokens: int
    chars: int
    pct_of_total: float = 0.0


@dataclass
class PromptTokenReport:
    """Full breakdown of a prompt's token budget.

    Attributes:
        session_id: Agent session this report belongs to (empty string if N/A).
        total_tokens: Estimated total token count.
        sections: Per-section breakdown, sorted descending by token count.
        system_prompt_tokens: Tokens in the system_prompt category.
        context_tokens: Tokens in the context category.
        user_prompt_tokens: Tokens in the user_prompt category.
        system_prompt_pct: Percentage of total tokens that are system prompt.
        context_pct: Percentage of total tokens that are context.
        user_prompt_pct: Percentage of total tokens that are user prompt.
        suggestions: Actionable reduction suggestions (empty when within budget).
    """

    session_id: str
    total_tokens: int
    sections: list[SectionTokens] = field(default_factory=list[SectionTokens])
    system_prompt_tokens: int = 0
    context_tokens: int = 0
    user_prompt_tokens: int = 0
    system_prompt_pct: float = 0.0
    context_pct: float = 0.0
    user_prompt_pct: float = 0.0
    suggestions: list[str] = field(default_factory=list[str])

    def summary(self) -> str:
        """Return a human-readable one-page summary.

        Returns:
            Multi-line string suitable for logging or CLI output.
        """
        lines = [
            f"## Prompt token usage (session: {self.session_id or 'N/A'})",
            f"Total tokens: ~{self.total_tokens:,}",
            "",
            f"{'Category':<40} {'Tokens':>8} {'Pct':>6}",
            "-" * 58,
            f"{'System prompt':<40} {self.system_prompt_tokens:>8,} {self.system_prompt_pct:>5.1f}%",
            f"{'Context':<40} {self.context_tokens:>8,} {self.context_pct:>5.1f}%",
            f"{'User prompt':<40} {self.user_prompt_tokens:>8,} {self.user_prompt_pct:>5.1f}%",
            "",
            "Top sections by token count:",
        ]
        for sec in self.sections[:10]:
            lines.append(
                f"  {sec.name:<36} {sec.tokens:>8,} ({sec.pct_of_total:>4.1f}%)"
            )
        if self.suggestions:
            lines += ["", "Suggestions:"]
            for s in self.suggestions:
                lines.append(f"  • {s}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for JSON storage."""
        return {
            "session_id": self.session_id,
            "total_tokens": self.total_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "context_tokens": self.context_tokens,
            "user_prompt_tokens": self.user_prompt_tokens,
            "system_prompt_pct": round(self.system_prompt_pct, 2),
            "context_pct": round(self.context_pct, 2),
            "user_prompt_pct": round(self.user_prompt_pct, 2),
            "sections": [asdict(s) for s in self.sections],
            "suggestions": self.suggestions,
        }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyse_prompt_sections(
    named_sections: list[tuple[str, str]],
    session_id: str = "",
) -> PromptTokenReport:
    """Compute a token-usage breakdown for a prompt's named sections.

    Args:
        named_sections: List of ``(section_name, content)`` pairs as produced
            by :func:`bernstein.core.spawn_prompt._render_prompt`.
        session_id: Agent session identifier (used for labelling only).

    Returns:
        :class:`PromptTokenReport` with per-section and per-category totals.
    """
    section_records: list[SectionTokens] = []
    for name, content in named_sections:
        tokens = estimate_tokens_for_text(content, assumed_type="text")
        section_records.append(
            SectionTokens(
                name=name,
                category=_section_category(name),
                tokens=tokens,
                chars=len(content),
            )
        )

    total = sum(s.tokens for s in section_records)

    # Fill in percentages
    for sec in section_records:
        sec.pct_of_total = (sec.tokens / total * 100.0) if total > 0 else 0.0

    # Sort descending by token count
    section_records.sort(key=lambda s: s.tokens, reverse=True)

    # Aggregate per category
    cat_tokens: dict[str, int] = {"system_prompt": 0, "context": 0, "user_prompt": 0}
    for sec in section_records:
        cat_tokens[sec.category] += sec.tokens

    def _pct(n: int) -> float:
        return (n / total * 100.0) if total > 0 else 0.0

    # Build suggestions
    suggestions: list[str] = []
    for cat, rec_max in _RECOMMENDED_PCT.items():
        actual_pct = _pct(cat_tokens[cat])
        if actual_pct > rec_max:
            # Find the biggest section(s) in this category
            big = [s for s in section_records if s.category == cat][:3]
            big_names = ", ".join(s.name for s in big)
            suggestions.append(
                f"{_CATEGORY_LABELS[cat]} is {actual_pct:.0f}% (recommended ≤{rec_max:.0f}%). "
                f"Largest sections: {big_names}."
            )

    return PromptTokenReport(
        session_id=session_id,
        total_tokens=total,
        sections=section_records,
        system_prompt_tokens=cat_tokens["system_prompt"],
        context_tokens=cat_tokens["context"],
        user_prompt_tokens=cat_tokens["user_prompt"],
        system_prompt_pct=_pct(cat_tokens["system_prompt"]),
        context_pct=_pct(cat_tokens["context"]),
        user_prompt_pct=_pct(cat_tokens["user_prompt"]),
        suggestions=suggestions,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_prompt_token_report(
    report: PromptTokenReport,
    workdir: Path,
) -> Path:
    """Write a prompt token report to ``.sdd/metrics/``.

    Args:
        report: The report to persist.
        workdir: Project root directory.

    Returns:
        Path to the written file.
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    name = f"prompt_token_usage_{report.session_id}.json" if report.session_id else "prompt_token_usage.json"
    out_path = metrics_dir / name
    try:
        out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        logger.debug("Prompt token report written: %s", out_path)
    except OSError as exc:
        logger.warning("Failed to write prompt token report: %s", exc)
    return out_path
