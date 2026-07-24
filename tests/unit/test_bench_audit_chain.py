"""Hermetic coverage for the audit-chain micro-benchmark (issue #2690).

The benchmark itself is a measurement tool, but its harness must stay correct
and reproducible: the segmented chain it builds has to be a genuine, verifiable
HMAC chain, and the append comparison has to actually isolate the chain's
marginal cost. These tests run the harness with tiny parameters so they are
fast and add no network or real-adapter dependency.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from bernstein.core.audit import AuditLog

# The benchmark lives under scripts/, which is not on the test import path
# (pytest only adds src/). Load it by file path, matching how other tests pull
# in script modules (e.g. test_format_release_notes_utm.py). The module is
# registered in sys.modules before execution so its @dataclass decorators can
# resolve their defining module on Python 3.14.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bench_audit_chain.py"
_spec = importlib.util.spec_from_file_location("_bench_audit_chain", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)

BENCH_KEY = bench.BENCH_KEY
bench_append = bench.bench_append
bench_verify = bench.bench_verify
build_segmented_chain = bench.build_segmented_chain
make_details = bench.make_details
render_markdown = bench.render_markdown
run_benchmark = bench.run_benchmark


def test_make_details_sizes_are_ordered() -> None:
    """Payload sizes grow small < medium < large so the axis is meaningful."""
    import json

    sizes = [len(json.dumps(make_details(s))) for s in ("small", "medium", "large")]
    assert sizes[0] < sizes[1] < sizes[2]


def test_bench_append_isolates_chain_cost(tmp_path: Path) -> None:
    """chain-on writes the same rows plus a constant HMAC overhead."""
    result = bench_append(tmp_path, "medium", n=40, warmup=5)

    assert result.chain_on["n"] == 40
    assert result.plain_append["n"] == 40
    # The chain adds the two 64-hex chain fields (prev_hmac + hmac) to every
    # row: a fixed per-entry byte cost independent of payload size.
    assert result.chain_byte_overhead > 100
    # chain-on does strictly more work per append than a bare line write.
    assert result.chain_on_bytes_per_entry > result.plain_bytes_per_entry


def test_chain_byte_overhead_is_constant_across_sizes(tmp_path: Path) -> None:
    """The chain's per-entry byte cost does not depend on payload size."""
    small = bench_append(tmp_path / "s", "small", n=20, warmup=2)
    large = bench_append(tmp_path / "l", "large", n=20, warmup=2)
    assert abs(small.chain_byte_overhead - large.chain_byte_overhead) < 1.0


def test_build_segmented_chain_is_a_real_verifiable_chain(tmp_path: Path) -> None:
    """A segmented chain the harness builds passes a real verify()."""
    audit_dir = tmp_path / "audit"
    build_segmented_chain(audit_dir, events=60, segments=3)

    segments = sorted(audit_dir.glob("*.jsonl"))
    assert len(segments) == 3

    ok, errors = AuditLog(audit_dir, key=BENCH_KEY).verify()
    assert ok, errors

    # The same chain scans clean and yields every event.
    result = AuditLog(audit_dir, key=BENCH_KEY).scan_verified()
    assert result.ok
    assert len(result.events) == 60


def test_bench_verify_reports_positive_throughput(tmp_path: Path) -> None:
    """The verify harness produces non-zero, well-formed throughput figures."""
    v = bench_verify(tmp_path, events=200, segments=2)
    assert v.events == 200
    assert v.segments == 2
    assert v.verify_events_per_s > 0
    assert v.scan_cold_events_per_s > 0
    assert v.scan_warm_tail_us > 0


def test_run_benchmark_renders_without_error(tmp_path: Path) -> None:
    """The end-to-end harness runs and renders a Markdown report."""
    report = run_benchmark(
        tmp_path,
        append_n=20,
        sizes=("small",),
        verify_points=((100, 1),),
    )
    assert len(report.append) == 1
    assert len(report.verify) == 1
    md = render_markdown(report)
    assert "Append latency" in md
    assert "Verify / scan throughput" in md
