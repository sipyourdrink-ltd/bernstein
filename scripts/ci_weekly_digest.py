"""Aggregate a week of main-branch CI signal into one actionable digest.

Consumed by ``.github/workflows/ci-weekly-digest.yml``. Reads workflow-run
records as JSONL (``--runs-file`` or stdin) plus the rolled-up
``auto-release-skipped`` issue numbers, and emits:

* a Markdown body (``--body-file``) for the weekly tracking issue, whose
  TL;DR always leads with ``recommended_action`` -- there is no separate
  alert path gated on ``has_signal``, and
* a compact JSON summary on stdout (including ``has_signal``) for the
  workflow run log.

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
import io
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

REAL_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out"})
CANCELLED_CONCLUSIONS = frozenset({"cancelled"})
SCHEDULED_EVENTS = frozenset({"schedule"})
DEFAULT_CHRONIC_THRESHOLD = 2

QUALITY_WORKFLOW_FILE = "static-analysis-extended.yml"
QUALITY_ARTIFACT_NAMES: tuple[str, str, str] = ("vulture-sarif", "refurb-sarif", "perflint-sarif")
_QUALITY_ARTIFACT_SET = frozenset(QUALITY_ARTIFACT_NAMES)
QUALITY_ELIGIBLE_EVENTS = frozenset({"schedule", "workflow_dispatch"})
QUALITY_MAX_AGE_DAYS = 8
_MAX_ARTIFACT_MEMBER_BYTES = 50 * 1024 * 1024
_PERFLINT_RULE_NAMES = {
    "W8101": "unnecessary-list-cast",
    "W8102": "incorrect-dictionary-iterator",
    "W8201": "loop-invariant-statement",
    "W8202": "loop-global-usage",
    "W8203": "loop-try-except",
    "W8204": "memoryview-over-bytes",
    "W8205": "dotted-import-in-loop",
    "W8206": "loop-object-attribute",
    "W8301": "use-tuple-over-list",
}
_PERFLINT_CLI_NOISE_RE = re.compile(
    r"^Command line:\d+: \[W0012\(unknown-option-value\), \] Unknown option value for '--enable',"
)
_PERFLINT_LINE_RE = re.compile(
    r"^[^:]+:\d+(?::\d+)?:\s*(?:\[(?P<code>[^(\],:\r\n]+)(?:\([^)]*\))?[^\]]*\]|(?P<pcode>[^:\s]+):)"
)
_RULE_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "vulture": re.compile(r"\Avulture/[a-z-]+\Z", re.ASCII),
    "refurb": re.compile(r"\AFURB[0-9]{3}\Z", re.ASCII),
    "perflint": re.compile(r"\AW8[0-9]{3}\Z", re.ASCII),
}


class _QualityValidationError(ValueError):
    """Raised when a quality artifact violates tool or rule-ID allowlists."""


@dataclass(frozen=True)
class QualityRunMeta:
    """Source static-analysis (extended) workflow run metadata."""

    run_id: int
    event: str
    created_at: str
    head_sha: str
    html_url: str


@dataclass(frozen=True)
class QualityReport:
    """Validated non-security quality counts or fail-closed omission status."""

    status: str  # "ok", "stale", "rejected", "unavailable"
    run: QualityRunMeta | None = None
    vulture_counts: dict[str, int] | None = None
    refurb_counts: dict[str, int] | None = None
    perflint_counts: dict[str, int] | None = None


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
    quality: QualityReport | None = None

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


def _parse_iso_utc(raw: str) -> datetime:
    s = raw.strip()
    dt = datetime.fromisoformat(f"{s[:-1]}+00:00" if s.endswith("Z") else s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def select_quality_artifacts(artifacts: Iterable[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Select only exact allowlisted quality artifacts, never security artifacts."""
    by_name: dict[str, dict[str, object]] = {}
    for artifact in artifacts:
        name = artifact.get("name")
        if not isinstance(name, str) or name not in _QUALITY_ARTIFACT_SET or bool(artifact.get("expired", False)):
            continue
        raw_id = artifact.get("id", 0)
        cand_id = int(raw_id) if isinstance(raw_id, (int, str)) and str(raw_id).isdigit() else 0
        existing = by_name.get(name)
        prev_id = int(existing["id"]) if existing and str(existing.get("id", "")).isdigit() else 0
        if existing is None or cand_id > prev_id:
            by_name[name] = artifact
    return {name: by_name[name] for name in QUALITY_ARTIFACT_NAMES if name in by_name}


def _check_rule(rule_id: object, pattern: re.Pattern[str], context: str) -> str:
    if not isinstance(rule_id, str) or pattern.fullmatch(rule_id) is None:
        raise _QualityValidationError(f"disallowed rule ID in {context}")
    return rule_id


def _parse_sarif_counts(raw_bytes: bytes, *, expected_tool: str, rule_pattern: re.Pattern[str]) -> Counter[str]:
    try:
        doc = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _QualityValidationError("invalid SARIF") from exc
    runs = doc.get("runs") if isinstance(doc, dict) else None
    if not isinstance(runs, list) or not runs:
        raise _QualityValidationError("SARIF missing runs")
    counts: Counter[str] = Counter()
    for run in runs:
        tool = run.get("tool") if isinstance(run, dict) else None
        driver = tool.get("driver") if isinstance(tool, dict) else None
        if not isinstance(driver, dict) or driver.get("name") != expected_tool:
            raise _QualityValidationError(f"unexpected SARIF tool in {expected_tool}")
        rules, results = driver.get("rules", []), run.get("results", [])
        if not isinstance(rules, list) or not isinstance(results, list):
            raise _QualityValidationError("invalid SARIF arrays")
        for r in rules:
            if isinstance(r, dict) and "id" in r:
                _check_rule(r["id"], rule_pattern, expected_tool)
        for res in results:
            if not isinstance(res, dict):
                raise _QualityValidationError("invalid SARIF result")
            counts[_check_rule(res.get("ruleId"), rule_pattern, expected_tool)] += 1
    return counts


def _parse_perflint_text(raw_bytes: bytes) -> Counter[str]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _QualityValidationError("invalid perflint encoding") from exc
    counts: Counter[str] = Counter()
    perflint_pattern = _RULE_ID_PATTERNS["perflint"]
    for line in (ln.strip() for ln in text.splitlines()):
        if not line or line.startswith("*************") or _PERFLINT_CLI_NOISE_RE.match(line):
            continue
        match = _PERFLINT_LINE_RE.match(line)
        if not match:
            raise _QualityValidationError("unexpected line format in perflint output")
        counts[_check_rule(match.group("code") or match.group("pcode"), perflint_pattern, "perflint")] += 1
    return counts


def _read_zip_members(zip_bytes: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                norm = info.filename.replace("\\", "/")
                if (
                    info.file_size > _MAX_ARTIFACT_MEMBER_BYTES
                    or norm.startswith("/")
                    or ".." in Path(norm).parts
                    or Path(norm).name in members
                ):
                    raise _QualityValidationError("unsafe or oversized artifact member")
                members[Path(norm).name] = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise _QualityValidationError("corrupt artifact zip") from exc
    return members


def parse_quality_artifact_files(files_by_artifact: dict[str, dict[str, bytes]], run: QualityRunMeta) -> QualityReport:
    """Validate and aggregate extracted quality artifact files (fail closed)."""
    if set(files_by_artifact) != _QUALITY_ARTIFACT_SET:
        return QualityReport("unavailable", run=run)
    try:
        parsed: dict[str, dict[str, int]] = {}
        for art, primary, tool in (
            ("vulture-sarif", "vulture.sarif", "vulture"),
            ("refurb-sarif", "refurb.sarif", "refurb"),
        ):
            files = files_by_artifact[art]
            sarifs = sorted(n for n in files if n.endswith(".sarif"))
            if primary not in files or not sarifs:
                raise _QualityValidationError(f"missing {primary}")
            for n in sarifs:
                counts = _parse_sarif_counts(files[n], expected_tool=tool, rule_pattern=_RULE_ID_PATTERNS[tool])
                if n == primary:
                    parsed[art] = dict(counts)
        p_files = files_by_artifact["perflint-sarif"]
        p_sarifs = sorted(n for n in p_files if n.endswith(".sarif"))
        sarif_counts = Counter()
        for n in p_sarifs:
            sarif_counts = _parse_sarif_counts(
                p_files[n], expected_tool="perflint", rule_pattern=_RULE_ID_PATTERNS["perflint"]
            )
        if "perflint.txt" in p_files:
            p_counts = dict(_parse_perflint_text(p_files["perflint.txt"]))
        elif p_sarifs:
            p_counts = dict(sarif_counts)
        else:
            raise _QualityValidationError("missing perflint output")
    except _QualityValidationError:
        return QualityReport("rejected", run=run)
    return QualityReport("ok", run, parsed["vulture-sarif"], parsed["refurb-sarif"], p_counts)


def _gh_api_bytes(endpoint: str) -> bytes:
    return subprocess.run(["gh", "api", endpoint], capture_output=True, check=True).stdout


def _select_latest_quality_run(runs: Iterable[dict[str, object]]) -> QualityRunMeta | None:
    best: tuple[datetime, QualityRunMeta] | None = None
    for item in runs:
        ev, st, created = str(item.get("event") or ""), str(item.get("status") or ""), str(item.get("created_at") or "")
        rid = item.get("id", 0)
        if st != "completed" or ev not in QUALITY_ELIGIBLE_EVENTS or not created or not str(rid).isdigit():
            continue
        meta = QualityRunMeta(
            int(str(rid)), ev, created, str(item.get("head_sha") or ""), str(item.get("html_url") or "")
        )
        cand = (_parse_iso_utc(created), meta)
        if best is None or (cand[0], cand[1].run_id) > (best[0], best[1].run_id):
            best = cand
    return best[1] if best else None


def fetch_quality_report(
    repo: str, *, now: datetime | None = None, api_get_bytes: Callable[[str], bytes] | None = None
) -> QualityReport:
    """Fetch newest completed weekly quality run and parse its 3 allowlisted artifacts."""
    fetch_bytes = api_get_bytes or _gh_api_bytes
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    candidates: list[dict[str, object]] = []
    try:
        for ev in sorted(QUALITY_ELIGIBLE_EVENTS):
            ep = (
                f"repos/{repo}/actions/workflows/{QUALITY_WORKFLOW_FILE}/runs"
                f"?branch=main&event={ev}&status=completed&per_page=5"
            )
            payload = json.loads(fetch_bytes(ep).decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("workflow_runs"), list):
                candidates.extend(r for r in payload["workflow_runs"] if isinstance(r, dict))
        run_meta = _select_latest_quality_run(candidates)
        if run_meta is None:
            return QualityReport("unavailable")
        if now_utc - _parse_iso_utc(run_meta.created_at) > timedelta(days=QUALITY_MAX_AGE_DAYS):
            return QualityReport("stale", run=run_meta)
        art_payload = json.loads(
            fetch_bytes(f"repos/{repo}/actions/runs/{run_meta.run_id}/artifacts?per_page=30").decode("utf-8")
        )
    except (subprocess.CalledProcessError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return QualityReport("unavailable")

    arts_raw = art_payload.get("artifacts") if isinstance(art_payload, dict) else None
    if not isinstance(arts_raw, list):
        return QualityReport("unavailable", run=run_meta)
    selected = select_quality_artifacts(a for a in arts_raw if isinstance(a, dict))
    if set(selected) != _QUALITY_ARTIFACT_SET:
        return QualityReport("unavailable", run=run_meta)
    files_by_artifact: dict[str, dict[str, bytes]] = {}
    for artifact_name in QUALITY_ARTIFACT_NAMES:
        aid = selected[artifact_name].get("id")
        if not isinstance(aid, (int, str)) or not str(aid).isdigit():
            return QualityReport("rejected", run=run_meta)
        try:
            zbytes = fetch_bytes(f"repos/{repo}/actions/artifacts/{int(aid)}/zip")
        except (subprocess.CalledProcessError, OSError, ValueError):
            return QualityReport("unavailable", run=run_meta)
        try:
            files_by_artifact[artifact_name] = _read_zip_members(zbytes)
        except _QualityValidationError:
            return QualityReport("rejected", run=run_meta)
    return parse_quality_artifact_files(files_by_artifact, run_meta)


def build_summary(
    runs: list[Run],
    skipped_issues: list[int],
    week_label: str,
    since: str,
    lookback_days: str,
    chronic_threshold: int = DEFAULT_CHRONIC_THRESHOLD,
    quality: QualityReport | None = None,
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
        quality=quality,
    )


def _fmt_rules(counts: dict[str, int], limit: int, *, detail: bool = False, named: bool = False) -> str:
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    if not top:
        return "_none_"
    if not detail:
        return ", ".join(f"`{rid}` ({n:,})" for rid, n in top)
    return ", ".join(
        f"`{rid}` (`{_PERFLINT_RULE_NAMES[rid]}`): {n:,}"
        if named and rid in _PERFLINT_RULE_NAMES
        else f"`{rid}`: {n:,}"
        for rid, n in top
    )


def render_quality_section(report: QualityReport) -> str:
    """Render advisory weekly code quality summary or one neutral omission line."""
    if report.status == "rejected":
        return "_Weekly code quality summary omitted: quality artifacts could not be validated._"
    if report.status == "stale":
        suffix = f" ({report.run.created_at[:10]}, `{report.run.head_sha[:7]}`)" if report.run else ""
        return (
            f"_Weekly code quality summary omitted: newest finished quality run{suffix} "
            f"is older than {QUALITY_MAX_AGE_DAYS} days._"
        )
    v_c, r_c, p_c = report.vulture_counts, report.refurb_counts, report.perflint_counts
    if report.status != "ok" or not report.run or v_c is None or r_c is None or p_c is None:
        return "_Weekly code quality summary omitted: no recent quality run available._"

    ev_lbl = "scheduled run" if report.run.event == "schedule" else "manual run"
    v_tot, r_tot, p_tot = sum(v_c.values()), sum(r_c.values()), sum(p_c.values())
    r_word = "rule" if len(r_c) == 1 else "rules"
    specs = (
        ("vulture", "Dead code (`--min-confidence 70`)", v_tot, v_c, "vulture-sarif"),
        ("refurb", "Python idioms", r_tot, r_c, "refurb-sarif"),
        ("perflint", "Hot-path antipatterns (`W8xxx`)", p_tot, p_c, "perflint-sarif"),
    )
    rows = [
        f"| `{tool}` | {focus} | {tot:,} | {_fmt_rules(cnt, 5)} | `{art}` |" for tool, focus, tot, cnt, art in specs
    ]
    src = (
        f"- Source: `static-analysis (extended)` {ev_lbl} [{report.run.run_id}]({report.run.html_url}) "
        f"({report.run.created_at[:10]}, `{report.run.head_sha[:7]}`)"
    )
    return "\n".join(
        [
            "## Weekly code quality summary (advisory)",
            "",
            src,
            "- Scope: non-security quality-linter artifacts only (`vulture-sarif`, `refurb-sarif`, `perflint-sarif`)",
            "",
            "| linter | focus | findings | top rule IDs | artifact |",
            "|---|---|---:|---|---|",
            *rows,
            "",
            "<details><summary>Top quality rule breakdown</summary>",
            "",
            f"- **`vulture` (`{v_tot:,}` total):** {_fmt_rules(v_c, 10, detail=True)}",
            f"- **`refurb` (`{r_tot:,}` total across `{len(r_c)}` {r_word}):** {_fmt_rules(r_c, 10, detail=True)}",
            f"- **`perflint` (`{p_tot:,}` `W8xxx` findings):** {_fmt_rules(p_c, 10, detail=True, named=True)}",
            "",
            "</details>",
            "",
            "_Advisory counts across `src/` only. These three weekly jobs do not gate PR merges._",
        ]
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

    if summary.quality is not None:
        add(render_quality_section(summary.quality))
        add("")

    add("---")
    add(
        "_Aggregated automatically by `ci-weekly-digest.yml`: one weekly tracking issue in place of "
        "per-event CI-drift issues, so main-branch failure patterns stay visible without cluttering the "
        "tracker. Previous weeks' digests auto-close when this one publishes._"
    )
    return "\n".join(lines) + "\n"


def summary_json(summary: DigestSummary) -> dict[str, object]:
    top = summary.top_offender
    payload: dict[str, object] = {
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
    if summary.quality is not None:
        payload["quality_status"] = summary.quality.status
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the weekly CI digest body + summary.")
    parser.add_argument("--runs-file", help="JSONL of workflow-run records; default stdin")
    parser.add_argument("--skipped", default="", help="whitespace/comma-separated issue numbers")
    parser.add_argument("--week-label", required=True, help="ISO week label, e.g. 2026-W28")
    parser.add_argument("--since", required=True, help="ISO timestamp of the window start")
    parser.add_argument("--lookback-days", default="7")
    parser.add_argument("--chronic-threshold", type=int, default=DEFAULT_CHRONIC_THRESHOLD)
    parser.add_argument("--quality-repo", help="GitHub owner/repo to fetch non-security quality artifacts")
    parser.add_argument("--now", help="Override current UTC ISO timestamp (for deterministic runs)")
    parser.add_argument("--body-file", required=True, help="where to write the Markdown body")
    args = parser.parse_args(argv)

    if args.runs_file:
        with open(args.runs_file, encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    else:
        raw_lines = sys.stdin.readlines()

    now_utc = _parse_iso_utc(args.now) if args.now else None
    quality = fetch_quality_report(args.quality_repo, now=now_utc) if args.quality_repo else None

    runs = parse_runs(raw_lines)
    skipped = parse_issue_numbers(args.skipped)
    summary = build_summary(
        runs,
        skipped,
        week_label=args.week_label,
        since=args.since,
        lookback_days=args.lookback_days,
        chronic_threshold=args.chronic_threshold,
        quality=quality,
    )

    with open(args.body_file, "w", encoding="utf-8") as handle:
        handle.write(render_body(summary))

    json.dump(summary_json(summary), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
