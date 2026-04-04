"""Framework context report: Bernstein vs. CrewAI and LangGraph.

Provides structured data types and report generation for comparing Bernstein's
architecture against popular multi-agent frameworks.

The public report generated from this module intentionally avoids publishing
numeric cross-framework benchmark claims until Bernstein can reproduce those
systems under a Bernstein-owned live harness.

The competitor metric constants remain available for internal analysis, tests,
and future work, but public report rendering excludes those numeric rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

Framework = Literal["bernstein", "crewai", "langgraph"]


@dataclass(frozen=True)
class CompetitorProfile:
    """Static description of a multi-agent framework's architecture.

    Args:
        name: Short identifier used in tables and comparisons.
        display_name: Human-readable display name.
        framework: Framework identifier tag.
        orchestration_model: Whether a live LLM drives task routing.
        scheduling_overhead_pct: Estimated fraction of total cost attributable
            to orchestration LLM calls (0.0 = none, 0.15 = ~15%).
        supports_any_cli_agent: Whether the framework works with arbitrary
            CLI agents (vs. requiring a specific SDK or model family).
        state_persistence: How state survives between tasks.
        description: One-sentence summary.
    """

    name: str
    display_name: str
    framework: Framework
    orchestration_model: bool
    scheduling_overhead_pct: float
    supports_any_cli_agent: bool
    state_persistence: str
    description: str

    def scheduling_overhead_label(self) -> str:
        """Human-readable scheduling overhead label."""
        if self.scheduling_overhead_pct == 0:
            return "none (deterministic code)"
        return "present (LLM-based routing)"


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Performance metrics for one framework on SWE-Bench Lite.

    Args:
        framework_name: Matches CompetitorProfile.name.
        model_config: Model(s) used (e.g. "3x claude-sonnet-4-6").
        swe_bench_resolve_rate: Fraction of SWE-Bench Lite instances resolved.
        swe_bench_resolved: Absolute count of resolved instances.
        swe_bench_total: Total instances attempted.
        mean_cost_per_issue_usd: Mean API cost per issue in USD.
        scheduling_cost_per_issue_usd: Cost attributed to orchestration overhead.
        mean_wall_time_s: Mean wall-clock seconds per issue.
        data_source: Where this data comes from.
        is_simulated: True if results are synthetic rather than real runs.
    """

    framework_name: str
    model_config: str
    swe_bench_resolve_rate: float
    swe_bench_resolved: int
    swe_bench_total: int
    mean_cost_per_issue_usd: float
    scheduling_cost_per_issue_usd: float
    mean_wall_time_s: float
    data_source: str
    is_simulated: bool = False

    @property
    def agent_cost_per_issue_usd(self) -> float:
        """Cost per issue excluding orchestration overhead."""
        return self.mean_cost_per_issue_usd - self.scheduling_cost_per_issue_usd

    @property
    def resolve_pct(self) -> str:
        """Formatted resolve rate as a percentage string."""
        return f"{self.swe_bench_resolve_rate * 100:.1f}%"


@dataclass
class HeadToHeadComparison:
    """A comparison between Bernstein and one or more competitor frameworks.

    Args:
        title: Report title.
        date: ISO-8601 date string of the comparison.
        profiles: Framework profiles indexed by name.
        metrics: SWE-Bench metrics indexed by name (may use name+variant key).
    """

    title: str
    date: str
    profiles: dict[str, CompetitorProfile] = field(default_factory=dict)
    metrics: dict[str, BenchmarkMetrics] = field(default_factory=dict)

    def cost_ratio(self, baseline_name: str, comparison_name: str) -> float | None:
        """Compute cost ratio between two framework configurations.

        Returns comparison / baseline, or None if either is missing.

        Args:
            baseline_name: Key in self.metrics for the cheaper system.
            comparison_name: Key in self.metrics for the more expensive system.

        Returns:
            Float ratio (e.g. 3.5 means comparison is 3.5x more expensive),
            or None if either key is missing.
        """
        b = self.metrics.get(baseline_name)
        c = self.metrics.get(comparison_name)
        if b is None or c is None:
            return None
        if b.mean_cost_per_issue_usd == 0:
            return None
        return c.mean_cost_per_issue_usd / b.mean_cost_per_issue_usd

    def resolve_rate_delta_pp(self, a_name: str, b_name: str) -> float | None:
        """Return resolve rate of a minus b in percentage points.

        Args:
            a_name: Metrics key for system A.
            b_name: Metrics key for system B.

        Returns:
            Delta in percentage points (positive = A is better), or None.
        """
        a = self.metrics.get(a_name)
        b = self.metrics.get(b_name)
        if a is None or b is None:
            return None
        return (a.swe_bench_resolve_rate - b.swe_bench_resolve_rate) * 100


# ---------------------------------------------------------------------------
# Canonical profiles
# ---------------------------------------------------------------------------

BERNSTEIN_PROFILE = CompetitorProfile(
    name="bernstein",
    display_name="Bernstein",
    framework="bernstein",
    orchestration_model=False,
    scheduling_overhead_pct=0.0,
    supports_any_cli_agent=True,
    state_persistence="file-based (.sdd/)",
    description="Deterministic Python orchestrator for short-lived CLI coding agents.",
)

CREWAI_PROFILE = CompetitorProfile(
    name="crewai",
    display_name="CrewAI",
    framework="crewai",
    orchestration_model=True,
    scheduling_overhead_pct=0.12,
    supports_any_cli_agent=False,
    state_persistence="in-memory (process lifetime)",
    description="LLM-backed crew of role-playing agents coordinated by a manager LLM.",
)

LANGGRAPH_PROFILE = CompetitorProfile(
    name="langgraph",
    display_name="LangGraph",
    framework="langgraph",
    orchestration_model=True,
    scheduling_overhead_pct=0.08,
    supports_any_cli_agent=False,
    state_persistence="checkpoint store (LangChain)",
    description="Graph-based state machine where each node may invoke an LLM.",
)

# ---------------------------------------------------------------------------
# Canonical metrics  (SWE-Bench Lite, 300 instances)
# ---------------------------------------------------------------------------

# Bernstein — from benchmarks/swe_bench/results/ (simulated)
BERNSTEIN_SONNET_METRICS = BenchmarkMetrics(
    framework_name="bernstein",
    model_config="3x claude-sonnet-4-6 (analyst + implementer + qa)",
    swe_bench_resolve_rate=0.390,
    swe_bench_resolved=117,
    swe_bench_total=300,
    mean_cost_per_issue_usd=0.42,
    scheduling_cost_per_issue_usd=0.00,
    mean_wall_time_s=197,
    data_source="benchmarks/swe_bench/results/bernstein-sonnet_summary.json (simulated)",
    is_simulated=True,
)

BERNSTEIN_MIXED_METRICS = BenchmarkMetrics(
    framework_name="bernstein",
    model_config="Haiku analyst, Sonnet implementer, Haiku qa",
    swe_bench_resolve_rate=0.373,
    swe_bench_resolved=112,
    swe_bench_total=300,
    mean_cost_per_issue_usd=0.16,
    scheduling_cost_per_issue_usd=0.00,
    mean_wall_time_s=177,
    data_source="benchmarks/swe_bench/results/bernstein-mixed_summary.json (simulated)",
    is_simulated=True,
)

# CrewAI — estimated from community SWE-Bench reports and GPT-4 pricing
# Sources: princeton-nlp/SWE-bench leaderboard (archived), community benchmarks
# on crewai-tools GitHub issues, and OpenAI GPT-4 Turbo pricing ($10/$30 per 1M tokens).
CREWAI_GPT4_METRICS = BenchmarkMetrics(
    framework_name="crewai",
    model_config="GPT-4 Turbo (manager + 3 worker agents)",
    swe_bench_resolve_rate=0.265,
    swe_bench_resolved=80,
    swe_bench_total=300,
    mean_cost_per_issue_usd=1.10,
    scheduling_cost_per_issue_usd=0.13,
    mean_wall_time_s=310,
    data_source=(
        "Estimated from community reports (crewai-tools/issues, r/MachineLearning). "
        "CrewAI does not publish official SWE-Bench numbers."
    ),
    is_simulated=False,
)

# LangGraph — estimated from LangChain published evals and Claude Sonnet pricing
# Sources: LangChain blog posts, SWE-bench leaderboard community submissions,
# and Anthropic Claude Sonnet pricing ($3/$15 per 1M tokens).
LANGGRAPH_SONNET_METRICS = BenchmarkMetrics(
    framework_name="langgraph",
    model_config="claude-sonnet-4-6 (ReAct graph, 3 nodes)",
    swe_bench_resolve_rate=0.305,
    swe_bench_resolved=92,
    swe_bench_total=300,
    mean_cost_per_issue_usd=0.55,
    scheduling_cost_per_issue_usd=0.04,
    mean_wall_time_s=245,
    data_source=(
        "Estimated from LangChain eval blog posts and community SWE-Bench runs. "
        "LangGraph does not publish official SWE-Bench Lite figures."
    ),
    is_simulated=False,
)

# ---------------------------------------------------------------------------
# Canonical comparison object
# ---------------------------------------------------------------------------

CANONICAL_COMPARISON = HeadToHeadComparison(
    title="Bernstein Benchmark Status and Framework Context",
    date="2026-03-31",
    profiles={
        "bernstein": BERNSTEIN_PROFILE,
        "crewai": CREWAI_PROFILE,
        "langgraph": LANGGRAPH_PROFILE,
    },
    metrics={
        "bernstein-sonnet": BERNSTEIN_SONNET_METRICS,
        "bernstein-mixed": BERNSTEIN_MIXED_METRICS,
        "crewai-gpt4": CREWAI_GPT4_METRICS,
        "langgraph-sonnet": LANGGRAPH_SONNET_METRICS,
    },
)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_architecture_table(comparison: HeadToHeadComparison) -> str:
    """Generate a markdown architecture comparison table.

    Args:
        comparison: The head-to-head comparison object.

    Returns:
        Markdown string with an architecture comparison table.
    """
    lines: list[str] = []
    lines.append("| Feature | Bernstein | CrewAI | LangGraph |")
    lines.append("|---------|-----------|--------|-----------|")

    profiles = comparison.profiles
    b = profiles.get("bernstein", BERNSTEIN_PROFILE)
    c = profiles.get("crewai", CREWAI_PROFILE)
    l = profiles.get("langgraph", LANGGRAPH_PROFILE)  # noqa: E741

    def yes_no(val: bool) -> str:
        return "Yes" if val else "No"

    rows: list[tuple[str, str, str, str]] = [
        (
            "Orchestration via LLM",
            yes_no(b.orchestration_model),
            yes_no(c.orchestration_model),
            yes_no(l.orchestration_model),
        ),
        (
            "Scheduling overhead",
            b.scheduling_overhead_label(),
            c.scheduling_overhead_label(),
            l.scheduling_overhead_label(),
        ),
        (
            "Works with any CLI agent",
            yes_no(b.supports_any_cli_agent),
            yes_no(c.supports_any_cli_agent),
            yes_no(l.supports_any_cli_agent),
        ),
        (
            "State persistence",
            b.state_persistence,
            c.state_persistence,
            l.state_persistence,
        ),
    ]

    for row in rows:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |")

    return "\n".join(lines)


def generate_swe_bench_table(comparison: HeadToHeadComparison) -> str:
    """Generate a public-safe publication-status table.

    Args:
        comparison: The head-to-head comparison object.

    Returns:
        Markdown string with publication-status information.
    """
    lines: list[str] = []
    lines.append("| System | Public numeric benchmark status | Notes |")
    lines.append("|--------|--------------------------------|-------|")
    lines.append(
        "| Bernstein | Published only from verified `benchmarks/swe_bench/run.py eval` artifacts | "
        "Public v1 scope is Bernstein vs solo baselines on SWE-Bench Lite. |"
    )
    if "crewai" in comparison.profiles:
        lines.append(
            "| CrewAI | Withheld from public numeric tables | "
            "No Bernstein-owned live harness is published yet. Architecture context only. |"
        )
    if "langgraph" in comparison.profiles:
        lines.append(
            "| LangGraph | Withheld from public numeric tables | "
            "No Bernstein-owned live harness is published yet. Architecture context only. |"
        )

    return "\n".join(lines)


def _short_source(source: str) -> str:
    """Shorten a data source string to fit in a table cell."""
    if "simulated" in source.lower():
        return "local (simulated)"
    if "estimated" in source.lower():
        return "estimated (community)"
    return source[:40]


def generate_key_findings(comparison: HeadToHeadComparison) -> str:
    """Generate a bullet-point key findings section.

    Args:
        comparison: The head-to-head comparison object.

    Returns:
        Markdown string with key findings.
    """
    lines: list[str] = []
    if "bernstein" in comparison.profiles:
        lines.append(
            "- Bernstein keeps orchestration in deterministic Python code, "
            "which removes the need for a manager-model control plane."
        )
    if "crewai" in comparison.profiles or "langgraph" in comparison.profiles:
        lines.append(
            "- CrewAI and LangGraph stay in this report as architecture context only. "
            "Public numeric rankings are withheld until Bernstein can reproduce them "
            "under a Bernstein-owned live harness."
        )
    if comparison.metrics:
        lines.append(
            "- Internal benchmark preview data may exist, but public publication is "
            "limited to verified SWE-Bench eval artifacts and Bernstein-vs-solo baselines."
        )

    if not lines:
        lines.append("- Comparison data not available — run benchmarks to populate.")

    return "\n".join(lines)


def generate_full_report(comparison: HeadToHeadComparison) -> str:
    """Generate a complete publishable markdown comparison report.

    Args:
        comparison: The head-to-head comparison to render.

    Returns:
        Markdown string of the full report.
    """
    arch_table = generate_architecture_table(comparison)
    swe_table = generate_swe_bench_table(comparison)
    findings = generate_key_findings(comparison)

    sections: list[str] = []
    sections.append(f"# {comparison.title}")
    sections.append("")
    sections.append("> **NOTE:** Public numeric framework-vs-framework rankings are intentionally withheld.")
    sections.append("> Bernstein publishes benchmark claims only from verified SWE-Bench eval artifacts,")
    sections.append("> and current public scope is Bernstein vs solo baselines rather than CrewAI/LangGraph tables.")
    sections.append("")
    sections.append(f"**Date:** {comparison.date}")
    sections.append("")
    sections.append("## TL;DR")
    sections.append("")
    sections.append(
        "> Bernstein keeps CrewAI and LangGraph on this page as architecture context.\n"
        "> Public benchmark publication is gated on verified SWE-Bench eval artifacts, not simulated or estimated rows."
    )

    sections.append("")
    sections.append("## Architecture Comparison")
    sections.append("")
    sections.append(arch_table)
    sections.append("")
    sections.append("## Public Benchmark Publication Status")
    sections.append("")
    sections.append(swe_table)
    sections.append("")
    sections.append("## Key Findings")
    sections.append("")
    sections.append(findings)
    sections.append("")

    return "\n".join(sections)
