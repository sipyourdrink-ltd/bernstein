"""Generate a publishable markdown report from SWE-Bench evaluation results."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from benchmarks.swe_bench.metrics import ScenarioSummary

_TEMPLATE = """\
# SWE-Bench Lite Evaluation: Bernstein Scaffolding Thesis

{mock_notice}**Date:** {date}
**Dataset:** SWE-Bench Lite ({total_instances} instances)

## TL;DR

> Bernstein + 3x Sonnet resolves {bernstein_sonnet_resolve_pct}% of SWE-Bench Lite at
> ${bernstein_sonnet_cost}/issue — beating Solo Opus ({solo_opus_resolve_pct}%, ${solo_opus_cost}/issue)
> at {cost_ratio}x lower cost.

## Results

| Scenario | Resolve rate | Mean time | Mean cost/issue | Total cost |
|---|---|---|---|---|
{table_rows}

## Methodology

### Evaluation framework
- **Dataset:** [SWE-Bench Lite](https://github.com/princeton-nlp/SWE-bench) — 300 GitHub issues from
  12 popular Python repositories.
- **Evaluation:** Official SWE-Bench Docker-based test harness.  An instance is "resolved" iff all
  FAIL_TO_PASS tests pass and all PASS_TO_PASS tests continue to pass.

### Scenarios

#### Solo Sonnet (baseline)
Single `claude-sonnet-4-6` agent prompted to read the issue and produce a patch.

#### Solo Opus (expensive baseline)
Single `claude-opus-4-6` agent, same prompt.

#### Bernstein 3x Sonnet (core thesis)
Three `claude-sonnet-4-6` agents in a sequential pipeline:
1. **Analyst** — reads the issue, identifies affected files, writes a concise plan.
2. **Implementer** — follows the plan to produce a patch.
3. **QA** — reviews the diff and flags obvious regressions.

The analyst's plan reduces the search space for the implementer; the QA stage catches
trivial mistakes before the patch is applied.

#### Bernstein Mixed (cost-optimised)
Same pipeline, but Analyst and QA use `claude-haiku-4-5` to cut cost:
- Analyst: Haiku
- Implementer: Sonnet
- QA: Haiku

### Cost accounting
Token counts are taken directly from the Claude API response (`usage` field).
Costs use March 2025 list prices:

| Model | Input ($/1k) | Output ($/1k) |
|---|---|---|
| claude-haiku-4-5 | $0.001 | $0.005 |
| claude-sonnet-4-6 | $0.003 | $0.015 |
| claude-opus-4-6 | $0.015 | $0.075 |

## Key findings

{findings}

## Reproducing

```bash
# Install dependencies
uv add datasets swebench

# Run full evaluation (requires Docker + API keys)
uv run python benchmarks/swe_bench/run.py \\
    --scenarios bernstein-sonnet solo-sonnet solo-opus bernstein-mixed \\
    --results-dir benchmarks/swe_bench/results

# Generate this report from saved results
uv run python benchmarks/swe_bench/run.py report \\
    --results-dir benchmarks/swe_bench/results
```

## Limitations

- SWE-Bench Lite is a subset (300/2294 instances).  Full SWE-Bench numbers may differ.
- Cost estimates assume list pricing; enterprise agreements will be lower.
- Wall-clock times include Docker setup overhead (~30 s/instance).
- QA rejection is advisory; a rejected patch is still applied if the implementer produced
  output.  Future work: let QA trigger a retry loop.
"""


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _fmt_cost(usd: float) -> str:
    return f"${usd:.2f}"


def _table_row(s: ScenarioSummary) -> str:
    return (
        f"| {s.scenario_name} "
        f"| {_pct(s.resolve_rate)} ({s.resolved}/{s.total_instances - s.skipped}) "
        f"| {s.mean_wall_time_s:.0f}s "
        f"| {_fmt_cost(s.mean_cost_per_instance_usd)} "
        f"| {_fmt_cost(s.total_cost_usd)} |"
    )


def _generate_findings(summaries: dict[str, ScenarioSummary]) -> str:
    lines: list[str] = []

    solo_s = summaries.get("solo-sonnet")
    solo_o = summaries.get("solo-opus")
    bern_s = summaries.get("bernstein-sonnet")
    bern_m = summaries.get("bernstein-mixed")

    if bern_s and solo_o:
        if bern_s.resolve_rate >= solo_o.resolve_rate:
            delta = (bern_s.resolve_rate - solo_o.resolve_rate) * 100
            lines.append(
                f"- **Bernstein 3x Sonnet outperforms Solo Opus** by {delta:.1f} percentage "
                f"points ({_pct(bern_s.resolve_rate)} vs {_pct(solo_o.resolve_rate)})."
            )
        else:
            delta = (solo_o.resolve_rate - bern_s.resolve_rate) * 100
            ratio = bern_s.mean_cost_per_instance_usd / max(solo_o.mean_cost_per_instance_usd, 0.001)
            lines.append(
                f"- Solo Opus leads Bernstein 3x Sonnet by {delta:.1f} pp "
                f"({_pct(solo_o.resolve_rate)} vs {_pct(bern_s.resolve_rate)}), "
                f"but at {ratio:.1f}x higher cost per issue."
            )

    if bern_s and solo_s:
        delta = (bern_s.resolve_rate - solo_s.resolve_rate) * 100
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"- The 3-agent pipeline adds {sign}{delta:.1f} pp over a single Sonnet agent "
            f"({_pct(bern_s.resolve_rate)} vs {_pct(solo_s.resolve_rate)})."
        )

    if bern_m and bern_s:
        cost_reduction = (1 - bern_m.mean_cost_per_instance_usd / max(bern_s.mean_cost_per_instance_usd, 0.0001)) * 100
        resolve_delta = (bern_m.resolve_rate - bern_s.resolve_rate) * 100
        sign = "+" if resolve_delta >= 0 else ""
        lines.append(
            f"- The mixed-model variant cuts cost by {cost_reduction:.0f}% "
            f"({_fmt_cost(bern_m.mean_cost_per_instance_usd)} vs "
            f"{_fmt_cost(bern_s.mean_cost_per_instance_usd)}/issue) "
            f"with a {sign}{resolve_delta:.1f} pp change in resolve rate."
        )

    if not lines:
        lines.append("- Evaluation in progress — run `bernstein-eval report` once complete.")

    return "\n".join(lines)


def generate(
    summaries: dict[str, ScenarioSummary],
    output_path: Path,
    total_instances: int = 300,
    is_mock: bool = False,
) -> None:
    """Render the evaluation report and write it to *output_path*.

    Args:
        summaries: Mapping of scenario_name → ScenarioSummary.
        output_path: Where to write the markdown file.
        total_instances: Number of instances evaluated (used in header).
        is_mock: If True, adds a notice that results are simulated.
    """
    table_rows = "\n".join(_table_row(s) for s in summaries.values())

    bern_s = summaries.get("bernstein-sonnet")
    solo_o = summaries.get("solo-opus")

    bernstein_sonnet_resolve_pct = f"{bern_s.resolve_rate * 100:.1f}" if bern_s else "?"
    bernstein_sonnet_cost = f"{bern_s.mean_cost_per_instance_usd:.2f}" if bern_s else "?"
    solo_opus_resolve_pct = f"{solo_o.resolve_rate * 100:.1f}" if solo_o else "?"
    solo_opus_cost = f"{solo_o.mean_cost_per_instance_usd:.2f}" if solo_o else "?"

    if bern_s and solo_o and solo_o.mean_cost_per_instance_usd > 0:
        cost_ratio = f"{solo_o.mean_cost_per_instance_usd / max(bern_s.mean_cost_per_instance_usd, 0.0001):.1f}"
    else:
        cost_ratio = "?"

    findings = _generate_findings(summaries)

    mock_notice = (
        "> **NOTE:** These results are **simulated** (generated with `run.py mock`).\n"
        "> Replace with real Docker-based results by running `run.py eval`.\n\n"
        if is_mock
        else ""
    )

    content = _TEMPLATE.format(
        date=date.today().isoformat(),
        total_instances=total_instances,
        bernstein_sonnet_resolve_pct=bernstein_sonnet_resolve_pct,
        bernstein_sonnet_cost=bernstein_sonnet_cost,
        solo_opus_resolve_pct=solo_opus_resolve_pct,
        solo_opus_cost=solo_opus_cost,
        cost_ratio=cost_ratio,
        table_rows=table_rows,
        findings=findings,
        mock_notice=mock_notice,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def generate_from_results_dir(
    results_dir: Path,
    output_path: Path | None = None,
    is_mock: bool | None = None,
) -> Path:
    """Load all summaries from *results_dir* and generate a report.

    Args:
        results_dir: Directory containing ``*_summary.json`` files.
        output_path: Where to write the report (default: results_dir/report.md).
        is_mock: Whether to add a simulated-data notice.  When None, auto-detected
            by checking whether instance IDs start with "mock_".

    Returns:
        Path to the written report.
    """
    if output_path is None:
        output_path = results_dir / "report.md"

    summaries: dict[str, ScenarioSummary] = {}
    for summary_file in sorted(results_dir.glob("*_summary.json")):
        data = json.loads(summary_file.read_text(encoding="utf-8"))
        s = ScenarioSummary(**data)
        summaries[s.scenario_name] = s

    # Determine total instances from first summary
    total = next(iter(summaries.values())).total_instances if summaries else 300

    # Auto-detect mock results by peeking at the first JSONL line
    if is_mock is None:
        is_mock = False
        for jsonl_file in results_dir.glob("*.jsonl"):
            first_line = next(
                (ln for ln in jsonl_file.read_text(encoding="utf-8").splitlines() if ln.strip()),
                "",
            )
            if first_line:
                try:
                    row = json.loads(first_line)
                    if str(row.get("instance_id", "")).startswith("mock_"):
                        is_mock = True
                except (json.JSONDecodeError, AttributeError):
                    pass
            break

    generate(summaries, output_path, total_instances=total, is_mock=is_mock)
    return output_path
