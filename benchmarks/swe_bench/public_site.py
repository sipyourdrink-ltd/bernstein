# ruff: noqa: E501

"""Public benchmark policy and artifact-driven docs generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from benchmarks.swe_bench.metrics import ScenarioSummary

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

PUBLIC_SCENARIO_ORDER: tuple[str, ...] = (
    "solo-sonnet",
    "solo-opus",
    "bernstein-sonnet",
    "bernstein-mixed",
)

SCENARIO_LABELS: dict[str, str] = {
    "solo-sonnet": "Solo Sonnet",
    "solo-opus": "Solo Opus",
    "bernstein-sonnet": "Bernstein 3x Sonnet",
    "bernstein-mixed": "Bernstein Mixed",
}

SCENARIO_PURPOSES: dict[str, str] = {
    "solo-sonnet": "Cheap single-agent baseline",
    "solo-opus": "Expensive single-agent baseline",
    "bernstein-sonnet": "All-Sonnet Bernstein pipeline",
    "bernstein-mixed": "Cost-optimized Bernstein pipeline",
}

ARCHITECTURE_CONTEXT_ROWS: tuple[tuple[str, str, str, str], ...] = (
    (
        "Routing control plane",
        "Deterministic Python scheduler",
        "Manager LLM plus worker agents",
        "Graph runtime with model-driven nodes",
    ),
    (
        "CLI agent compatibility",
        "Works with CLI coding agents",
        "SDK-centric framework",
        "Application framework, not CLI-agent orchestration",
    ),
    (
        "State model",
        "File-based `.sdd/` state",
        "Process/in-memory workflows",
        "Checkpoint store via LangChain runtime",
    ),
)


@dataclass(frozen=True)
class PublicBenchmarkContext:
    """Normalized public benchmark publication state."""

    summaries: dict[str, ScenarioSummary]
    ready: bool
    dataset: str | None
    sample_size: int | None
    commit_sha: str | None
    last_verified_run: str | None
    blockers: tuple[str, ...]

    @property
    def heading(self) -> str:
        if not self.ready or self.sample_size is None:
            return "Benchmark Status & Methodology"
        if self.sample_size < 300:
            return f"Verified Pilot Results (n={self.sample_size})"
        return f"Verified SWE-Bench Lite Results (n={self.sample_size})"

    @property
    def status_lines(self) -> tuple[str, ...]:
        if self.ready:
            lines = [
                "Verified public benchmark results: available",
                "Current page shows verified SWE-Bench Lite results and reproducibility details.",
            ]
            if self.last_verified_run:
                lines.append(f"Last verified run: {self.last_verified_run}")
            return tuple(lines)
        return (
            "Verified public benchmark results: in progress",
            "Current page shows methodology, harness coverage, and reproducibility path.",
            "Checked-in artifacts are treated as preview data until verified eval results are published.",
        )

    @property
    def required_summaries(self) -> list[ScenarioSummary]:
        return [self.summaries[name] for name in PUBLIC_SCENARIO_ORDER if name in self.summaries]


def load_summaries(results_dir: Path) -> dict[str, ScenarioSummary]:
    """Load every summary JSON from *results_dir*."""
    summaries: dict[str, ScenarioSummary] = {}
    for summary_file in sorted(results_dir.glob("*_summary.json")):
        data = json.loads(summary_file.read_text(encoding="utf-8"))
        summary = ScenarioSummary.from_dict(data)
        summaries[summary.scenario_name] = summary
    return summaries


def build_public_context(summaries: dict[str, ScenarioSummary]) -> PublicBenchmarkContext:
    """Build publication status from raw scenario summaries."""
    blockers: list[str] = []

    missing = [name for name in PUBLIC_SCENARIO_ORDER if name not in summaries]
    if missing:
        blockers.append(f"Missing required scenarios: {', '.join(missing)}.")

    required = [summaries[name] for name in PUBLIC_SCENARIO_ORDER if name in summaries]

    if required and any(not summary.is_verified_public_result for summary in required):
        blockers.append("At least one required scenario is not marked as a verified `eval` artifact.")

    dataset = _common_nonempty(summary.dataset for summary in required)
    if required and dataset is None:
        blockers.append("Required scenarios do not agree on dataset provenance.")

    sample_size = _common_int(summary.sample_size for summary in required)
    if required and sample_size is None:
        blockers.append("Required scenarios do not agree on sample size.")

    commit_sha = _common_nonempty(summary.commit_sha for summary in required)
    if required and commit_sha is None:
        blockers.append("Required scenarios do not carry one shared commit SHA.")

    last_verified_run = _latest_nonempty(summary.run_at for summary in required)
    if required and not last_verified_run:
        blockers.append("Required scenarios are missing run timestamps.")

    ready = not blockers and len(required) == len(PUBLIC_SCENARIO_ORDER)
    return PublicBenchmarkContext(
        summaries=summaries,
        ready=ready,
        dataset=dataset,
        sample_size=sample_size,
        commit_sha=commit_sha,
        last_verified_run=last_verified_run if ready else None,
        blockers=tuple(blockers),
    )


def _md_ready_section(context: PublicBenchmarkContext, lines: list[str]) -> None:
    """Append verified results section to markdown lines."""
    lines.append(f"## {context.heading}")
    lines.append("")
    lines.append(f"**Dataset:** {context.dataset}")
    lines.append(f"**Commit:** `{context.commit_sha}`")
    if context.last_verified_run:
        lines.append(f"**Last verified run:** {context.last_verified_run}")
    lines.append("")
    lines.append("| Scenario | Resolve rate | Mean time | Mean cost/issue | Total cost | Model family |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for summary in context.required_summaries:
        lines.append(
            f"| {SCENARIO_LABELS.get(summary.scenario_name, summary.scenario_name)} "
            f"| {_pct(summary.resolve_rate)} "
            f"| {summary.mean_wall_time_s:.0f}s "
            f"| ${summary.mean_cost_per_instance_usd:.2f} "
            f"| ${summary.total_cost_usd:.2f} "
            f"| {summary.model_family or 'n/a'} |"
        )
    lines.append("")


def _md_artifact_section(context: PublicBenchmarkContext, lines: list[str]) -> None:
    """Append artifact state section to markdown lines."""
    lines.append("## Current Artifact State")
    lines.append("")
    lines.append("| Scenario | Source type | Verified | Sample size | Notes |")
    lines.append("|---|---|---|---:|---|")
    for name in PUBLIC_SCENARIO_ORDER:
        summary = context.summaries.get(name)
        if summary is None:
            lines.append(f"| {SCENARIO_LABELS.get(name, name)} | missing | No | 0 | Awaiting artifact |")
            continue
        lines.append(
            f"| {SCENARIO_LABELS.get(name, name)} "
            f"| {summary.source_type} "
            f"| {'Yes' if summary.verified else 'No'} "
            f"| {summary.sample_size or summary.total_instances} "
            f"| {summary.notes or 'Preview artifact'} |"
        )
    lines.append("")
    if context.blockers:
        lines.append("## Publication Blockers")
        lines.append("")
        for blocker in context.blockers:
            lines.append(f"- {blocker}")
        lines.append("")


def render_public_markdown(context: PublicBenchmarkContext) -> str:
    """Render a safe public benchmark markdown report."""
    lines: list[str] = []
    lines.append("# SWE-Bench Lite Benchmarks")
    lines.append("")
    for line in context.status_lines:
        lines.append(f"> **Status:** {line}" if line.startswith("Verified") else f"> {line}")
    lines.append("")

    if context.ready:
        _md_ready_section(context, lines)
    else:
        _md_artifact_section(context, lines)

    lines.append("## Public Benchmark Policy")
    lines.append("")
    lines.append(
        "- Only `benchmarks/swe_bench/run.py eval` artifacts marked `verified=true` are eligible for public benchmark claims."
    )
    lines.append("- Public v1 comparisons are limited to Bernstein vs real single-agent baselines on SWE-Bench Lite.")
    lines.append(
        "- Competitor framework content stays qualitative until Bernstein can reproduce those systems with a Bernstein-owned live harness."
    )
    lines.append("")
    lines.append("## Harness Coverage")
    lines.append("")
    lines.append("| Scenario | Purpose |")
    lines.append("|---|---|")
    for name in PUBLIC_SCENARIO_ORDER:
        lines.append(f"| {SCENARIO_LABELS.get(name, name)} | {SCENARIO_PURPOSES.get(name, 'n/a')} |")
    lines.append("")
    lines.append("## Reproducing")
    lines.append("")
    lines.append("```bash")
    lines.append("# Simulation/modeling harnesses (preview only, not public benchmark claims)")
    lines.append("uv run python benchmarks/run_benchmark.py")
    lines.append("uv run python benchmarks/run_benchmark.py --issues-file benchmarks/issues.json")
    lines.append("")
    lines.append("# Verified evaluation harness for public benchmark publication")
    lines.append("uv run python benchmarks/swe_bench/run.py eval \\")
    lines.append("    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \\")
    lines.append("    --limit 50")
    lines.append("")
    lines.append("# Generate benchmark markdown and the docs page from saved artifacts")
    lines.append("uv run python benchmarks/swe_bench/run.py report")
    lines.append("uv run python scripts/generate_benchmark_docs.py")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _html_verified_results_section(context: PublicBenchmarkContext) -> str:
    """Build the verified results HTML section."""
    result_rows = "\n".join(
        "<tr>"
        f"<td>{escape(SCENARIO_LABELS.get(s.scenario_name, s.scenario_name))}</td>"
        f"<td>{escape(s.model_family or 'n/a')}</td>"
        f"<td>{_pct(s.resolve_rate)}</td>"
        f"<td>{s.mean_wall_time_s:.0f}s</td>"
        f"<td>${s.mean_cost_per_instance_usd:.2f}</td>"
        f"<td>${s.total_cost_usd:.2f}</td>"
        "</tr>"
        for s in context.required_summaries
    )
    return f"""
  <section id="verified-results" style="padding: 0; margin-bottom: var(--space-16);">
    <h2>{escape(context.heading)}</h2>
    <p>Dataset: {escape(context.dataset or "SWE-Bench Lite")} &middot; Commit: <code>{escape(context.commit_sha or "")}</code> &middot; Last verified run: {escape(context.last_verified_run or "")}</p>
    <div class="comparison-wrap">
      <table class="comparison-table">
        <thead><tr><th>Scenario</th><th>Model family</th><th>Resolve rate</th><th>Mean time</th><th>Mean cost/issue</th><th>Total cost</th></tr></thead>
        <tbody>{result_rows}</tbody>
      </table>
    </div>
  </section>
"""


def _html_preview_results_section(context: PublicBenchmarkContext) -> str:
    """Build the preview/artifact state HTML section."""
    preview_parts: list[str] = []
    for name in PUBLIC_SCENARIO_ORDER:
        summary = context.summaries.get(name)
        source_type = escape(summary.source_type if summary is not None else "missing")
        verified = "Yes" if summary is not None and summary.verified else "No"
        sample_count = (summary.sample_size or summary.total_instances) if summary is not None else 0
        notes = escape(summary.notes if summary is not None and summary.notes else "Awaiting artifact")
        preview_parts.append(
            f"<tr><td>{escape(SCENARIO_LABELS.get(name, name))}</td>"
            f"<td>{source_type}</td><td>{verified}</td>"
            f"<td>{sample_count}</td><td>{notes}</td></tr>"
        )
    preview_rows = "\n".join(preview_parts)
    blocker_html = "".join(f"<li>{escape(b)}</li>" for b in context.blockers)
    return f"""
  <section id="artifact-state" style="padding: 0; margin-bottom: var(--space-16);">
    <h2>Current Artifact State</h2>
    <p>This page suppresses headline benchmark claims until all four public scenarios are present as verified SWE-Bench eval artifacts.</p>
    <div class="comparison-wrap">
      <table class="comparison-table">
        <thead><tr><th>Scenario</th><th>Source type</th><th>Verified</th><th>Sample size</th><th>Notes</th></tr></thead>
        <tbody>{preview_rows}</tbody>
      </table>
    </div>
    <div class="callout callout-info" style="margin-top: var(--space-4);">
      <strong>Publication blockers</strong>
      <ul>{blocker_html}</ul>
    </div>
  </section>
"""


def render_public_html(context: PublicBenchmarkContext) -> str:
    """Render the public benchmark page for ``docs/leaderboard.html``."""
    cards = [
        ("Publication status", context.status_lines[0]),
        ("Public claim source", "benchmarks/swe_bench/run.py eval only"),
        (
            "Publication scope",
            "Bernstein vs solo baselines on SWE-Bench Lite until third-party live harnesses exist.",
        ),
        (
            "Next publication target" if not context.ready else "Current publication tier",
            "Verified Pilot Results (n=50)" if not context.ready else context.heading,
        ),
    ]
    card_html = "\n".join(
        (
            '<div class="metric-card">'
            f'<div class="metric-label">{escape(label)}</div>'
            f'<div class="metric-copy">{escape(value)}</div>'
            "</div>"
        )
        for label, value in cards
    )

    status_html = "".join(f"<li>{escape(line)}</li>" for line in context.status_lines)

    results_section = (
        _html_verified_results_section(context) if context.ready else _html_preview_results_section(context)
    )

    architecture_rows = "\n".join(
        (
            "<tr>"
            f"<td>{escape(feature)}</td>"
            f"<td>{escape(bernstein)}</td>"
            f"<td>{escape(crewai)}</td>"
            f"<td>{escape(langgraph)}</td>"
            "</tr>"
        )
        for feature, bernstein, crewai, langgraph in ARCHITECTURE_CONTEXT_ROWS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Benchmarks — Bernstein</title>
  <meta name="description" content="Benchmark status, methodology, and verified SWE-Bench publication policy for Bernstein." />
  <link rel="stylesheet" href="style.css" />
  <link id="hljs-theme" rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css" />
  <script src="script.js"></script>
  <style>
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: var(--space-6);
      margin-bottom: var(--space-12);
    }}
    .metric-card {{
      padding: var(--space-6);
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: var(--radius);
    }}
    .metric-label {{
      font-size: var(--text-sm);
      color: var(--text-3);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: var(--space-2);
    }}
    .metric-copy {{
      font-size: var(--text-lg);
      color: var(--text-1);
      line-height: 1.5;
    }}
    .status-banner {{
      padding: var(--space-6);
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: var(--radius);
      margin-bottom: var(--space-10);
    }}
    .status-banner ul {{
      margin: 0;
      padding-left: 1.25rem;
    }}
    .comparison-table th {{
      background: var(--bg-2);
      text-align: left;
    }}
  </style>
</head>
<body>
<nav class="nav">
  <div class="nav-inner">
    <a href="index.html" class="nav-logo">bernstein</a>
    <div class="nav-links">
      <a href="index.html" class="nav-link" data-page="index.html">Overview</a>
      <a href="getting-started.html" class="nav-link" data-page="getting-started.html">Getting Started</a>
      <a href="concepts.html" class="nav-link" data-page="concepts.html">Concepts</a>
      <a href="adapters.html" class="nav-link" data-page="adapters.html">Adapters</a>
      <a href="api.html" class="nav-link" data-page="api.html">API Reference</a>
      <a href="leaderboard.html" class="nav-link active" data-page="leaderboard.html">Benchmarks</a>
      <a href="docs-index.html" class="nav-link" data-page="docs-index.html">All Docs</a>
    </div>
    <div class="nav-actions">
      <button class="btn-icon" id="theme-toggle" aria-label="Toggle theme">
        <span id="theme-icon">&#9681;</span>
      </button>
      <a href="https://github.com/chernistry/bernstein" class="btn-primary" target="_blank" rel="noopener">
        GitHub &#8599;
      </a>
      <button class="nav-toggle" id="nav-toggle" aria-label="Toggle menu">&#9776;</button>
    </div>
  </div>
</nav>

<div class="page-hero">
  <div class="container">
    <div class="section-label">Benchmarks</div>
    <h1>Benchmark Status &amp; Methodology</h1>
    <p>Bernstein publishes benchmark claims only from verified SWE-Bench eval artifacts. Simulation and modeling harnesses remain available for previewing methodology and internal capacity planning.</p>
  </div>
</div>

<div class="container" style="margin-top: var(--space-12); margin-bottom: var(--space-16);">
  <div class="status-banner">
    <h2 style="margin-top: 0;">Publication Status</h2>
    <ul>{status_html}</ul>
  </div>

  <div class="metric-grid">
    {card_html}
  </div>

{results_section}

  <section id="scope" style="padding: 0; margin-bottom: var(--space-16);">
    <h2>V1 Publication Scope</h2>
    <p>Public benchmark publication is intentionally narrow in this phase: Bernstein vs real single-agent baselines on SWE-Bench Lite.</p>
    <div class="comparison-wrap">
      <table class="comparison-table">
        <thead>
          <tr>
            <th>Scenario</th>
            <th>Purpose</th>
          </tr>
        </thead>
        <tbody>
          {"".join(f"<tr><td>{escape(SCENARIO_LABELS[name])}</td><td>{escape(SCENARIO_PURPOSES[name])}</td></tr>" for name in PUBLIC_SCENARIO_ORDER)}
        </tbody>
      </table>
    </div>
  </section>

  <section id="architecture-context" style="padding: 0; margin-bottom: var(--space-16);">
    <h2>Framework Context</h2>
    <p>CrewAI and LangGraph remain in the docs as architecture context, not as public numeric benchmark rows, until Bernstein can reproduce them under a Bernstein-owned live harness.</p>
    <div class="comparison-wrap">
      <table class="comparison-table">
        <thead>
          <tr>
            <th>Feature</th>
            <th>Bernstein</th>
            <th>CrewAI</th>
            <th>LangGraph</th>
          </tr>
        </thead>
        <tbody>
          {architecture_rows}
        </tbody>
      </table>
    </div>
  </section>

  <section id="reproduce" style="padding: 0;">
    <h2>Reproduce</h2>
    <p>Simulation/modeling harnesses remain useful for workflow exploration, but only the SWE-Bench eval path is eligible for public benchmark publication.</p>
    <div class="code-block">
      <pre><code class="language-bash"># Simulation/modeling harnesses (preview only)
uv run python benchmarks/run_benchmark.py
uv run python benchmarks/run_benchmark.py --issues-file benchmarks/issues.json

# Verified evaluation harness for public benchmark publication
uv run python benchmarks/swe_bench/run.py eval \\
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \\
    --limit 50

# Generate markdown report and docs page from saved artifacts
uv run python benchmarks/swe_bench/run.py report
uv run python scripts/generate_benchmark_docs.py</code></pre>
    </div>
  </section>
</div>

<footer>
  <div class="container">
    <p>Bernstein &middot; <a href="index.html">Overview</a> &middot; <a href="https://github.com/chernistry/bernstein">GitHub</a> &middot; Apache 2.0</p>
  </div>
</footer>

<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>hljs.highlightAll();</script>
</body>
</html>
"""


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _common_nonempty(values: Iterable[str]) -> str | None:
    items = {value.strip() for value in values if value and value.strip()}
    if len(items) == 1:
        return next(iter(items))
    return None


def _common_int(values: Iterable[int]) -> int | None:
    items = {value for value in values if value > 0}
    if len(items) == 1:
        return next(iter(items))
    return None


def _latest_nonempty(values: Iterable[str]) -> str | None:
    items = sorted(value.strip() for value in values if value and value.strip())
    if not items:
        return None
    return items[-1]
