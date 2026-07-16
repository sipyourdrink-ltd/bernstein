"""Aggregate a week of main-branch CI signal into one actionable digest.

Consumed by ``.github/workflows/ci-weekly-digest.yml``. Reads workflow-run
records as JSONL (``--runs-file`` or stdin) plus the rolled-up
``auto-release-skipped`` issue numbers, and emits:

* a Markdown body (``--body-file``) for the weekly tracking issue,
* an optional short alert text (``--alert-file``) written only when there
  is a real signal, and
* a compact JSON summary on stdout that the workflow parses to decide
  whether to raise a threshold alert.

Why this exists in the shape it does:

* Only conclusion ``failure``/``timed_out`` count as real red. Runs with
  conclusion ``cancelled`` are concurrency-superseded the overwhelming
  majority of the time (``cancel-in-progress`` on rapid pushes, superseded
  schedule ticks), so they are reported separately as informational and
  never inflate the headline failure count. The previous inline aggregation
  counted cancelled runs as ``main-red`` and reported ~7x the real number.
* Runs are split by trigger (``schedule`` vs push/other) so a chronically
  red nightly is distinguishable from a one-off push failure that a PR
  author already saw.
* Output ordering is fully deterministic (sorted), so the idempotent weekly
  upsert produces a byte-identical body on re-run and does not thrash the
  issue.

Stdlib only: runs on a bare ``ubuntu-latest`` runner without ``uv sync``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REAL_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out"})
CANCELLED_CONCLUSIONS = frozenset({"cancelled"})
SCHEDULED_EVENTS = frozenset({"schedule"})
DEFAULT_CHRONIC_THRESHOLD = 2


@dataclass(frozen=True)
class Run:
    """A single main-branch workflow run relevant to the digest."""

    run_id: int
    conclusion: str
    event: str
    name: str
    sha: str
    url: str

    @property
    def is_scheduled(self) -> bool:
        return self.event in SCHEDULED_EVENTS

    @property
    def trigger(self) -> str:
        return "schedule" if self.is_scheduled else "push"

    @property
    def is_real_failure(self) -> bool:
        return self.conclusion in REAL_FAILURE_CONCLUSIONS

    @property
    def is_cancelled(self) -> bool:
        return self.conclusion in CANCELLED_CONCLUSIONS


def parse_runs(raw_lines: Iterable[str]) -> list[Run]:
    """Parse JSONL workflow-run records, de-duplicating by run id.

    Malformed / blank lines are skipped. Runs are keyed by ``id`` so a run
    that appears on more than one API page (pagination overlap) is counted
    exactly once.
    """
    runs: dict[int, Run] = {}
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        obj = json.loads(line)
        rid = int(obj.get("id", 0) or 0)
        runs[rid] = Run(
            run_id=rid,
            conclusion=str(obj.get("conclusion") or ""),
            event=str(obj.get("event") or ""),
            name=str(obj.get("name") or "(unnamed workflow)"),
            sha=str(obj.get("head_sha") or "")[:7],
            url=str(obj.get("html_url") or ""),
        )
    return list(runs.values())


def parse_issue_numbers(raw: str) -> list[int]:
    """Parse a whitespace/comma-separated list of issue numbers (``#`` ok)."""
    out: list[int] = []
    for tok in raw.replace(",", " ").split():
        stripped = tok.lstrip("#")
        if stripped.isdigit():
            out.append(int(stripped))
    return out


@dataclass
class DigestSummary:
    """Deterministic, classified view of a week of main-branch CI."""

    week_label: str
    since: str
    lookback_days: str
    real_failures: list[Run]
    cancelled: list[Run]
    skipped_issues: list[int]
    chronic_threshold: int

    @property
    def real_failure_count(self) -> int:
        return len(self.real_failures)

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_issues)

    @property
    def scheduled_failure_count(self) -> int:
        return sum(1 for r in self.real_failures if r.is_scheduled)

    @property
    def failures_by_workflow_total(self) -> Counter[str]:
        return Counter(r.name for r in self.real_failures)

    @property
    def failures_by_workflow(self) -> list[tuple[str, str, int]]:
        """``(workflow, trigger, count)`` rows, most-failing first then A-Z."""
        counter: Counter[tuple[str, str]] = Counter((r.name, r.trigger) for r in self.real_failures)
        return sorted(
            ((wf, trig, n) for (wf, trig), n in counter.items()),
            key=lambda row: (-row[2], row[0], row[1]),
        )

    @property
    def chronic_red(self) -> list[tuple[str, int]]:
        """Workflows at or above the chronic-failure threshold this window."""
        return sorted(
            ((wf, n) for wf, n in self.failures_by_workflow_total.items() if n >= self.chronic_threshold),
            key=lambda row: (-row[1], row[0]),
        )

    @property
    def top_offender(self) -> tuple[str, int] | None:
        counts = self.failures_by_workflow_total
        if not counts:
            return None
        return sorted(counts.items(), key=lambda row: (-row[1], row[0]))[0]

    @property
    def has_signal(self) -> bool:
        """True when the window contains at least one real failure."""
        return self.real_failure_count > 0

    @property
    def recommended_action(self) -> str:
        if self.real_failure_count == 0:
            if self.cancelled_count:
                return (
                    f"No real failures. {self.cancelled_count} cancelled run(s) are "
                    "concurrency-superseded (informational) - no action needed."
                )
            return "Clean week - no failed or cancelled runs on main."
        chronic = self.chronic_red
        if chronic:
            wf, n = chronic[0]
            return f"Chronically red: `{wf}` failed {n}x this window. Assign an owner to root-cause or quarantine it."
        if self.scheduled_failure_count:
            sched = sorted({r.name for r in self.real_failures if r.is_scheduled})
            names = ", ".join(f"`{w}`" for w in sched)
            return (
                f"Scheduled workflow(s) failing: {names}. "
                "Scheduled red hides from PR authors - triage before it becomes chronic."
            )
        top = self.top_offender
        assert top is not None  # non-empty because real_failure_count > 0
        wf, n = top
        return f"Top offender: `{wf}` ({n} real failure(s)). Triage the most recent failing run."


def build_summary(
    runs: list[Run],
    skipped_issues: list[int],
    week_label: str,
    since: str,
    lookback_days: str,
    chronic_threshold: int = DEFAULT_CHRONIC_THRESHOLD,
) -> DigestSummary:
    real = sorted(
        (r for r in runs if r.is_real_failure),
        key=lambda r: (r.name, r.trigger, r.sha, r.run_id),
    )
    cancelled = sorted(
        (r for r in runs if r.is_cancelled),
        key=lambda r: (r.name, r.trigger, r.sha, r.run_id),
    )
    return DigestSummary(
        week_label=week_label,
        since=since,
        lookback_days=lookback_days,
        real_failures=real,
        cancelled=cancelled,
        skipped_issues=sorted(set(skipped_issues), reverse=True),
        chronic_threshold=chronic_threshold,
    )


def render_body(summary: DigestSummary) -> str:
    lines: list[str] = []
    add = lines.append

    add("## TL;DR")
    add("")
    add(f"- Window: last {summary.lookback_days} day(s) (since {summary.since})")
    add(f"- Real CI failures on main: **{summary.real_failure_count}** (conclusion `failure`/`timed_out`)")
    add(f"- Superseded/cancelled runs: {summary.cancelled_count} (usually concurrency - informational)")
    add(f"- auto-release-skipped issues rolled up: {summary.skipped_count}")
    add(f"- **Recommended action:** {summary.recommended_action}")
    add("")

    add("## Real failures by workflow")
    add("")
    if summary.real_failure_count == 0:
        add("_No real failures in the window._")
    else:
        add("| workflow | trigger | failures |")
        add("|---|---|---|")
        for wf, trig, n in summary.failures_by_workflow:
            add(f"| {wf} | {trig} | {n} |")
        if summary.chronic_red:
            add("")
            chronic = ", ".join(f"`{wf}` ({n}x)" for wf, n in summary.chronic_red)
            add(f"> **Chronically red (>= {summary.chronic_threshold} failures):** {chronic}")
        add("")
        add("<details><summary>Failed run list</summary>")
        add("")
        add("| conclusion | trigger | workflow | sha | url |")
        add("|---|---|---|---|---|")
        for r in summary.real_failures:
            add(f"| {r.conclusion} | {r.trigger} | {r.name} | `{r.sha}` | {r.url} |")
        add("")
        add("</details>")
    add("")

    if summary.cancelled_count:
        add("<details><summary>Superseded / cancelled runs (informational)</summary>")
        add("")
        add("| trigger | workflow | sha | url |")
        add("|---|---|---|---|")
        for r in summary.cancelled:
            add(f"| {r.trigger} | {r.name} | `{r.sha}` | {r.url} |")
        add("")
        add("</details>")
        add("")

    add("## auto-release-skipped issues rolled up")
    add("")
    if summary.skipped_count == 0:
        add("_None._")
    else:
        for n in summary.skipped_issues:
            add(f"- #{n}")
    add("")

    add("---")
    add(
        "_Aggregated automatically by `ci-weekly-digest.yml`: one weekly tracking issue in place of "
        "per-event CI-drift issues, so main-branch failure patterns stay visible without cluttering the "
        "tracker. Previous weeks' digests auto-close when this one publishes._"
    )
    return "\n".join(lines) + "\n"


def render_alert(summary: DigestSummary) -> str:
    head = f"CI weekly digest {summary.week_label}: {summary.real_failure_count} real failure(s) on main"
    return f"{head}\n{summary.recommended_action}"


def summary_json(summary: DigestSummary) -> dict[str, object]:
    top = summary.top_offender
    return {
        "week_label": summary.week_label,
        "real_failure_count": summary.real_failure_count,
        "cancelled_count": summary.cancelled_count,
        "scheduled_failure_count": summary.scheduled_failure_count,
        "skipped_count": summary.skipped_count,
        "chronic_red": [{"workflow": wf, "failures": n} for wf, n in summary.chronic_red],
        "top_offender": ({"workflow": top[0], "failures": top[1]} if top else None),
        "has_signal": summary.has_signal,
        "recommended_action": summary.recommended_action,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the weekly CI digest body + summary.")
    parser.add_argument("--runs-file", help="JSONL of workflow-run records; default stdin")
    parser.add_argument("--skipped", default="", help="whitespace/comma-separated issue numbers")
    parser.add_argument("--week-label", required=True, help="ISO week label, e.g. 2026-W28")
    parser.add_argument("--since", required=True, help="ISO timestamp of the window start")
    parser.add_argument("--lookback-days", default="7")
    parser.add_argument("--chronic-threshold", type=int, default=DEFAULT_CHRONIC_THRESHOLD)
    parser.add_argument("--body-file", required=True, help="where to write the Markdown body")
    parser.add_argument("--alert-file", help="write a short alert text here when has_signal is true")
    args = parser.parse_args(argv)

    if args.runs_file:
        with open(args.runs_file, encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    else:
        raw_lines = sys.stdin.readlines()

    runs = parse_runs(raw_lines)
    skipped = parse_issue_numbers(args.skipped)
    summary = build_summary(
        runs,
        skipped,
        week_label=args.week_label,
        since=args.since,
        lookback_days=args.lookback_days,
        chronic_threshold=args.chronic_threshold,
    )

    with open(args.body_file, "w", encoding="utf-8") as handle:
        handle.write(render_body(summary))

    if args.alert_file and summary.has_signal:
        with open(args.alert_file, "w", encoding="utf-8") as handle:
            handle.write(render_alert(summary))

    json.dump(summary_json(summary), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
