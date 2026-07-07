"""Live PR-open-path evidence wiring (issue #2362, AC3).

These tests exercise the *live* path -- ``load_session_summary`` reading a
session's sealed bundle off disk, then ``build_pr_body`` rendering it -- rather
than a hand-built :class:`EvidenceSummary`. When a wrap-up records the completed
task and that task sealed a bundle, the opened PR body carries the Evidence
section linking the bundle; when there is no bundle the block is omitted cleanly
so pre-existing PRs are unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.evidence.bundle import EvidenceProducer, run_evidence_gate
from bernstein.core.integrations.pr_gen import build_pr_body, load_session_summary

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"k" * 32


def _seal_bundle(workdir: Path, task_id: str) -> None:
    """Seal a deterministic, verifiable evidence bundle for ``task_id``."""

    def runner(_p: EvidenceProducer) -> tuple[int, bytes]:
        return 0, b"3 passed\n"

    run_evidence_gate(
        workdir=workdir,
        task_id=task_id,
        producers=(EvidenceProducer(name="tests", kind="test", command=("run",), required=True),),
        runner=runner,
        timestamp=1,
        hmac_key=_KEY,
    )


def _write_wrapup(workdir: Path, payload: dict[str, object]) -> None:
    sessions = workdir / ".sdd" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "1000-wrapup.json").write_text(json.dumps(payload), encoding="utf-8")


def test_live_pr_body_links_bundle_from_completed_task(tmp_path: Path) -> None:
    """A wrap-up naming a completed task with a sealed bundle yields an Evidence block."""
    _seal_bundle(tmp_path, "T-live-1")
    _write_wrapup(
        tmp_path,
        {"timestamp": 1000.0, "session_id": "sess-live", "goal": "add a feature", "completed_task_ids": ["T-live-1"]},
    )

    summary = load_session_summary(None, workdir=tmp_path)
    assert summary.evidence is not None
    assert summary.evidence.task_id == "T-live-1"

    body = build_pr_body(summary)
    assert "## Evidence" in body
    assert "bernstein evidence show T-live-1" in body
    # The anchor prefix of the sealed bundle is surfaced (not the evidence bytes).
    assert summary.evidence.anchor.split(":", 1)[-1][:16] in body


def test_live_pr_body_accepts_singular_task_id_key(tmp_path: Path) -> None:
    """A wrap-up carrying a singular ``task_id`` is also resolved."""
    _seal_bundle(tmp_path, "T-single")
    _write_wrapup(tmp_path, {"timestamp": 1000.0, "session_id": "s", "goal": "g", "task_id": "T-single"})

    summary = load_session_summary(None, workdir=tmp_path)
    assert summary.evidence is not None
    assert "## Evidence" in build_pr_body(summary)


def test_live_pr_body_omits_evidence_when_no_bundle(tmp_path: Path) -> None:
    """A completed task with no sealed bundle omits the Evidence block cleanly."""
    _write_wrapup(
        tmp_path,
        {"timestamp": 1000.0, "session_id": "sess-live", "goal": "add a feature", "completed_task_ids": ["T-none"]},
    )

    summary = load_session_summary(None, workdir=tmp_path)
    assert summary.evidence is None

    body = build_pr_body(summary)
    assert "## Evidence" not in body
    # Other sections remain present.
    assert "## Summary" in body
    assert "## Verification" in body


def test_live_pr_body_omits_evidence_when_wrapup_names_no_task(tmp_path: Path) -> None:
    """A wrap-up with no task identity never surfaces evidence, even if bundles exist."""
    _seal_bundle(tmp_path, "T-orphan")
    _write_wrapup(tmp_path, {"timestamp": 1000.0, "session_id": "sess-live", "goal": "add a feature"})

    summary = load_session_summary(None, workdir=tmp_path)
    assert summary.evidence is None
    assert "## Evidence" not in build_pr_body(summary)
