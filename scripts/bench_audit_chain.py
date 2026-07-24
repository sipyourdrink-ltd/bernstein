#!/usr/bin/env python3
"""Micro-benchmark: the cost of the HMAC-chained audit log (issue #2690).

The lineage spine and the replay journal are always on; the HMAC-chained audit
log is opt-in (``--audit`` / ``BERNSTEIN_AUDIT=1`` / a compliance preset). This
script measures what enabling it costs per scheduling decision so the
default-on decision rests on numbers rather than a guess.

It is hermetic and reproducible:

* No network, no real adapters, no orchestrator - only :class:`AuditLog`.
* A fixed HMAC key and fixed synthetic payloads, so re-running on the same host
  reproduces the same distribution (wall-clock figures vary with the machine).
* All I/O lands in a caller-supplied directory (a ``tmp_path`` under test, a
  ``TemporaryDirectory`` from :func:`main`); nothing touches the real
  ``.sdd/audit`` tree or the user's audit key.

Two questions, two comparisons:

* **Append.** ``chain_on`` is a real :meth:`AuditLog.log`. ``plain_append`` is
  the same canonical row written straight to a JSONL file with no ``prev_hmac``
  / ``hmac`` and no chain-tail recovery. The gap between them is the marginal
  cost attributable to the chain itself, on top of a line you would write
  anyway; ``chain_on`` alone is the absolute per-decision cost versus today's
  default of writing nothing.
* **Verify.** Full-chain :meth:`AuditLog.verify` and cold
  :meth:`AuditLog.scan_verified` walk every byte; a warm cursor scan re-reads
  only what was appended since. Reported as events/second so the number scales
  to any run length.

Run it directly to print a Markdown report and a JSON blob::

    uv run python scripts/bench_audit_chain.py
    uv run python scripts/bench_audit_chain.py --json-only > bench.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bernstein.core.audit import AuditLog

#: Fixed key so the benchmark never mints or reads a real audit key.
BENCH_KEY = b"benchmark-key-2690-deterministic"

#: A representative scheduling-decision event type and actor.
_EVENT_TYPE = "schedule.decision"
_ACTOR = "orchestrator"
_RESOURCE_TYPE = "task"


def make_details(size: str) -> dict[str, Any]:
    """Return a synthetic ``details`` payload of a given realistic size.

    The three sizes bracket what real scheduling and lifecycle events carry:

    * ``small`` - a bare transition (a handful of scalar fields).
    * ``medium`` - a scheduling decision with the chosen agent, model, and a
      short reason string.
    * ``large`` - a decision that embeds a truncated tool result or diff
      summary, the upper end of what lands in the chain.
    """
    if size == "small":
        return {"from": "queued", "to": "running", "attempt": 1}
    if size == "medium":
        return {
            "from": "queued",
            "to": "running",
            "attempt": 1,
            "agent": "backend-3",
            "model": "claude-sonnet",
            "reason": "highest-priority ready task on an idle worker",
            "queue_depth": 12,
        }
    if size == "large":
        return {
            "from": "review",
            "to": "done",
            "attempt": 2,
            "agent": "backend-3",
            "model": "claude-sonnet",
            "reason": "quality gate passed on retry",
            "queue_depth": 12,
            "tool_result_head": "x" * 800,
            "changed_files": [f"src/pkg/module_{i}.py" for i in range(12)],
        }
    raise ValueError(f"unknown size {size!r}")


def _summarize(latencies_ns: list[int]) -> dict[str, float]:
    """Reduce raw per-op nanosecond timings to microsecond summary stats."""
    micros = sorted(v / 1000.0 for v in latencies_ns)
    n = len(micros)
    p95 = micros[min(n - 1, round(0.95 * (n - 1)))]
    return {
        "n": float(n),
        "mean_us": statistics.fmean(micros),
        "median_us": statistics.median(micros),
        "p95_us": p95,
        "min_us": micros[0],
        "max_us": micros[-1],
    }


@dataclass
class AppendResult:
    """Append-latency and on-disk-size figures for one entry size."""

    size: str
    chain_on: dict[str, float]
    plain_append: dict[str, float]
    chain_on_bytes_per_entry: float
    plain_bytes_per_entry: float

    @property
    def marginal_mean_us(self) -> float:
        """Mean per-append cost attributable to the chain, in microseconds."""
        return self.chain_on["mean_us"] - self.plain_append["mean_us"]

    @property
    def chain_byte_overhead(self) -> float:
        """Extra bytes per entry the chain adds over a plain row."""
        return self.chain_on_bytes_per_entry - self.plain_bytes_per_entry


@dataclass
class VerifyResult:
    """Verify / scan throughput for one (events, segments) point."""

    events: int
    segments: int
    verify_events_per_s: float
    scan_cold_events_per_s: float
    scan_warm_tail_us: float


@dataclass
class BenchReport:
    """Full benchmark output: append + verify tables plus provenance."""

    append: list[AppendResult] = field(default_factory=list)
    verify: list[VerifyResult] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def bench_append(
    workdir: Path,
    size: str,
    *,
    n: int = 2000,
    warmup: int = 200,
) -> AppendResult:
    """Measure per-append latency and bytes/entry for chain-on vs plain-append.

    ``chain_on`` drives a real :class:`AuditLog`; ``plain_append`` writes the
    same canonical field set minus the two chain fields to an ordinary JSONL
    file, isolating the marginal cost of the HMAC chain from the file write
    every logged decision pays regardless.
    """
    details = make_details(size)

    # --- chain on: real AuditLog.log ---
    on_dir = workdir / f"on-{size}"
    log = AuditLog(on_dir, key=BENCH_KEY)
    for _ in range(warmup):
        log.log(_EVENT_TYPE, _ACTOR, _RESOURCE_TYPE, "warmup", details)
    on_lat: list[int] = []
    for i in range(n):
        start = time.perf_counter_ns()
        log.log(_EVENT_TYPE, _ACTOR, _RESOURCE_TYPE, f"task-{i}", details)
        on_lat.append(time.perf_counter_ns() - start)
    on_bytes = sum(p.stat().st_size for p in on_dir.glob("*.jsonl"))
    on_rows = warmup + n
    chain_on_bytes_per_entry = on_bytes / on_rows

    # --- chain off: plain canonical JSONL append, no HMAC, no recovery ---
    off_dir = workdir / f"off-{size}"
    off_dir.mkdir(parents=True, exist_ok=True)
    off_path = off_dir / "plain.jsonl"
    ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _row(resource_id: str) -> str:
        entry = {
            "timestamp": ts,
            "event_type": _EVENT_TYPE,
            "actor": _ACTOR,
            "resource_type": _RESOURCE_TYPE,
            "resource_id": resource_id,
            "details": details,
        }
        return json.dumps(entry, sort_keys=True) + "\n"

    for _ in range(warmup):
        with off_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(_row("warmup"))
    off_lat: list[int] = []
    for i in range(n):
        row = _row(f"task-{i}")
        start = time.perf_counter_ns()
        with off_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(row)
        off_lat.append(time.perf_counter_ns() - start)
    off_bytes = off_path.stat().st_size
    plain_bytes_per_entry = off_bytes / (warmup + n)

    return AppendResult(
        size=size,
        chain_on=_summarize(on_lat),
        plain_append=_summarize(off_lat),
        chain_on_bytes_per_entry=chain_on_bytes_per_entry,
        plain_bytes_per_entry=plain_bytes_per_entry,
    )


def build_segmented_chain(audit_dir: Path, *, events: int, segments: int) -> None:
    """Materialise one continuous HMAC chain split across ``segments`` files.

    Events are produced through the real :meth:`AuditLog.log` so every byte is
    identical to production output, then the single day's rows are partitioned
    into dated ``<YYYY-MM-DD>.jsonl`` files whose names sort in chain order.
    :meth:`AuditLog.verify` walks live files in sorted order, so the resulting
    multi-segment layout is a genuine cross-file chain - the same construction
    the audit test-suite uses to exercise the archive boundary.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=BENCH_KEY)
    details = make_details("medium")
    for i in range(events):
        log.log(_EVENT_TYPE, _ACTOR, _RESOURCE_TYPE, f"task-{i}", details)

    live = list(audit_dir.glob("*.jsonl"))
    if len(live) != 1:  # pragma: no cover - single-run invariant
        raise RuntimeError(f"expected one live file, found {[p.name for p in live]}")
    source = live[0]
    lines = source.read_text(encoding="utf-8").splitlines()
    source.unlink()

    # Even partition, remainder folded into the last segment.
    per = max(1, events // segments)
    base_day = datetime(2020, 1, 1, tzinfo=UTC)
    start = 0
    for seg in range(segments):
        end = events if seg == segments - 1 else min(events, start + per)
        chunk = lines[start:end]
        if not chunk:
            break
        day = (base_day + timedelta(days=seg)).strftime("%Y-%m-%d")
        (audit_dir / f"{day}.jsonl").write_text("\n".join(chunk) + "\n", encoding="utf-8")
        start = end


def _best_seconds(fn: Any, reps: int = 5) -> float:
    """Return the fastest wall-clock time over ``reps`` calls, in seconds."""
    best = float("inf")
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def bench_verify(workdir: Path, *, events: int, segments: int) -> VerifyResult:
    """Measure full-verify, cold-scan, and warm-cursor-scan cost of a chain."""
    audit_dir = workdir / f"verify-{events}-{segments}"
    build_segmented_chain(audit_dir, events=events, segments=segments)

    def _do_verify() -> None:
        ok, errors = AuditLog(audit_dir, key=BENCH_KEY).verify()
        if not ok:  # pragma: no cover - benchmark sanity guard
            raise RuntimeError(f"benchmark chain failed to verify: {errors[:3]}")

    verify_s = _best_seconds(_do_verify)

    def _do_cold_scan() -> None:
        result = AuditLog(audit_dir, key=BENCH_KEY).scan_verified()
        if not result.ok:  # pragma: no cover - benchmark sanity guard
            raise RuntimeError(f"benchmark chain failed to scan: {result.errors[:3]}")

    scan_cold_s = _best_seconds(_do_cold_scan)

    # Warm cursor: authenticate the whole chain once, append a single event,
    # then re-scan from the cursor - the live hot-path cost of an authenticated
    # read after one more decision.
    reader = AuditLog(audit_dir, key=BENCH_KEY)
    cursor = reader.scan_verified().cursor
    writer = AuditLog(audit_dir, key=BENCH_KEY)
    warm_samples: list[int] = []
    for i in range(20):
        writer.log(_EVENT_TYPE, _ACTOR, _RESOURCE_TYPE, f"warm-{i}", make_details("medium"))
        start = time.perf_counter_ns()
        res = reader.scan_verified(cursor)
        warm_samples.append(time.perf_counter_ns() - start)
        cursor = res.cursor

    return VerifyResult(
        events=events,
        segments=segments,
        verify_events_per_s=events / verify_s if verify_s > 0 else 0.0,
        scan_cold_events_per_s=events / scan_cold_s if scan_cold_s > 0 else 0.0,
        scan_warm_tail_us=statistics.median(warm_samples) / 1000.0,
    )


def run_benchmark(
    workdir: Path,
    *,
    append_n: int = 2000,
    sizes: tuple[str, ...] = ("small", "medium", "large"),
    verify_points: tuple[tuple[int, int], ...] = (
        (1000, 1),
        (10000, 1),
        (9000, 30),
        (9000, 90),
    ),
) -> BenchReport:
    """Run the full append + verify matrix into ``workdir`` and return results."""
    report = BenchReport()
    for size in sizes:
        report.append.append(bench_append(workdir, size, n=append_n))
    for events, segments in verify_points:
        report.verify.append(bench_verify(workdir, events=events, segments=segments))
    report.meta = {
        "issue": 2690,
        "append_n": append_n,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "generated_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "Wall-clock figures are host-dependent; ratios (chain-on vs "
            "plain-append) and bytes/entry are stable across hosts."
        ),
    }
    return report


def render_markdown(report: BenchReport) -> str:
    """Render a :class:`BenchReport` as a Markdown report."""
    lines: list[str] = []
    lines.append("### Append latency (per event)\n")
    lines.append("| entry size | chain-on mean | chain-on p95 | plain-append mean | chain marginal mean |")
    lines.append("|---|--:|--:|--:|--:|")
    for r in report.append:
        lines.append(
            f"| {r.size} | {r.chain_on['mean_us']:.2f} us | {r.chain_on['p95_us']:.2f} us "
            f"| {r.plain_append['mean_us']:.2f} us | {r.marginal_mean_us:+.2f} us |"
        )
    lines.append("\n### Bytes written per entry\n")
    lines.append("| entry size | chain-on | plain-append | chain overhead |")
    lines.append("|---|--:|--:|--:|")
    for r in report.append:
        lines.append(
            f"| {r.size} | {r.chain_on_bytes_per_entry:.0f} B | {r.plain_bytes_per_entry:.0f} B "
            f"| {r.chain_byte_overhead:+.0f} B |"
        )
    lines.append("\n### Verify / scan throughput\n")
    lines.append("| events | segments | verify (events/s) | cold scan (events/s) | warm-cursor tail |")
    lines.append("|--:|--:|--:|--:|--:|")
    for v in report.verify:
        lines.append(
            f"| {v.events} | {v.segments} | {v.verify_events_per_s:,.0f} "
            f"| {v.scan_cold_events_per_s:,.0f} | {v.scan_warm_tail_us:.1f} us |"
        )
    lines.append("")
    lines.append(
        f"_Generated {report.meta.get('generated_at', '?')} "
        f"on Python {report.meta.get('python', '?')} / {report.meta.get('platform', '?')}. "
        f"{report.meta.get('note', '')}_"
    )
    return "\n".join(lines)


def _report_to_dict(report: BenchReport) -> dict[str, Any]:
    return {
        "append": [asdict(r) for r in report.append],
        "verify": [asdict(v) for v in report.verify],
        "meta": report.meta,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--append-n", type=int, default=2000, help="appends measured per entry size")
    parser.add_argument("--json-only", action="store_true", help="emit only the JSON blob")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="bench-audit-2690-") as tmp:
        report = run_benchmark(Path(tmp), append_n=args.append_n)

    if args.json_only:
        print(json.dumps(_report_to_dict(report), indent=2))
    else:
        print(render_markdown(report))
        print("\n<details><summary>raw json</summary>\n")
        print("```json")
        print(json.dumps(_report_to_dict(report), indent=2))
        print("```\n</details>")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
