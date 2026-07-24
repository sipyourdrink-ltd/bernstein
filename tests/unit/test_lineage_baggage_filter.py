"""Ambient host trace context must not be sealed into the lineage chain (#2787).

``record_artifact_write`` reads the W3C ``BAGGAGE`` / ``TRACEPARENT`` /
``TRACESTATE`` environment variables and folds them into the HMAC-covered
lineage entry. Ambient values inherited from an unrelated launching process
(e.g. a Sentry/OTEL-instrumented shell) must be stripped before they enter the
entry hash or HMAC body; only orchestrator-controlled baggage members survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters.base import record_artifact_write
from bernstein.core.lineage.spine import LineageSpine, SpineStatus

_KEY = b"k" * 32
_SENTRY_BAGGAGE = (
    "sentry-environment=production,sentry-release=host-app@1.2.3,"
    "sentry-public_key=deadbeef,sentry-trace_id=abc123,sentry-org_id=42"
)


def _record(root: Path, run_id: str) -> LineageSpine:
    record_artifact_write(
        artifact_path="src/out.py",
        content=b"print('hi')\n",
        actor="agent:test",
        step_id="tc-1",
        model="claude",
        lineage_root=root,
        run_id=run_id,
        hmac_key=_KEY,
        timestamp=1,
    )
    return LineageSpine(root, run_id=run_id, hmac_key=_KEY)


def test_foreign_baggage_is_not_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient sentry-* baggage never reaches the spine entry or its pre-image."""
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    monkeypatch.setenv("BAGGAGE", _SENTRY_BAGGAGE)
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.delenv("TRACESTATE", raising=False)

    root = tmp_path / "lineage"
    spine = _record(root, "run-foreign")
    entries = list(spine.iter_entries())
    assert len(entries) == 1
    assert entries[0].baggage is None

    # The foreign identifiers must be absent from the raw persisted bytes,
    # which are exactly the HMAC/hash pre-image material.
    raw = (root / "run-foreign" / "spine.jsonl").read_bytes()
    assert b"sentry-" not in raw
    assert spine.verify().status is SpineStatus.OK


def test_bernstein_baggage_survives_and_foreign_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestrator-controlled members are kept; foreign members are dropped."""
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    monkeypatch.setenv("BAGGAGE", f"bernstein-run=r1,{_SENTRY_BAGGAGE}")
    monkeypatch.setenv("TRACEPARENT", "00-abc-123-01")
    monkeypatch.setenv("TRACESTATE", "vendor=1")

    root = tmp_path / "lineage"
    spine = _record(root, "run-mixed")
    entry = next(iter(spine.iter_entries()))
    assert entry.baggage == "bernstein-run=r1"
    # A bernstein-owned baggage member proves the trace context is ours, so
    # traceparent/tracestate are recorded.
    assert entry.traceparent == "00-abc-123-01"
    assert entry.tracestate == "vendor=1"
    raw = (root / "run-mixed" / "spine.jsonl").read_bytes()
    assert b"sentry-" not in raw
    assert spine.verify().status is SpineStatus.OK


def test_ambient_traceparent_dropped_without_bernstein_baggage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """traceparent/tracestate are dropped when no bernstein baggage is present."""
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    monkeypatch.delenv("BAGGAGE", raising=False)
    monkeypatch.setenv("TRACEPARENT", "00-foreignhost-999-01")
    monkeypatch.setenv("TRACESTATE", "sentry=1")

    root = tmp_path / "lineage"
    spine = _record(root, "run-ambient-tp")
    entry = next(iter(spine.iter_entries()))
    assert entry.traceparent is None
    assert entry.tracestate is None
    raw = (root / "run-ambient-tp" / "spine.jsonl").read_bytes()
    assert b"foreignhost" not in raw
