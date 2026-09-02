"""Unit tests for the ``absence_verified`` completion signal (issue #3650).

The signal is the absence-shaped member of ``completion_signals``: its value is
the ``tool_call_id`` of the call that reported "nothing found", and it passes
only when that call's recorded coverage payload hash-matches the lineage
coverage entry anchored to the *same* ``tool_call_id``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bernstein.core.lineage.coverage import anchor_coverage_record
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.quality.absence_coverage import verify_anchored_absence_claim
from bernstein.core.quality.janitor import evaluate_signal
from bernstein.core.tasks.models import CompletionSignal
from bernstein.core.tasks.task_store_core import _narrow_signal_type
from bernstein.core.tools.coverage import ToolCoverageRecord, compute_corpus_digest

_RUN_ID = "run-abs-1"
_TASK_ID = "T-abs"
_AGENT_ID = "agent-abs"


def _complete_record() -> ToolCoverageRecord:
    return ToolCoverageRecord(
        file_count=2,
        corpus_digest=compute_corpus_digest(["a.py", "b.py"]),
        coverage="complete",
        truncated=False,
        truncation_reason=None,
        exit_status=0,
        exit_checked=True,
    )


def _write_tool_call(workdir: Path, call_id: str, coverage: dict[str, object] | None) -> Path:
    """Append one tool-call record under the run's instrumentation tree."""
    agent_dir = workdir / ".sdd" / "runs" / _RUN_ID / "tasks" / _TASK_ID / "agents" / _AGENT_ID
    agent_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "call_id": call_id,
        "ts_start": "2026-09-02T00:00:00Z",
        "ts_end": "2026-09-02T00:00:01Z",
        "tool": "grep",
        "args": {"pattern": "TODO"},
        "success": True,
        "error": None,
        "result": "",
    }
    if coverage is not None:
        record["coverage"] = coverage
    path = agent_dir / "tool-calls.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path


def _anchor(workdir: Path, call_id: str, record: ToolCoverageRecord) -> LineageStore:
    lineage_root = workdir / ".sdd" / "lineage"
    lineage_root.mkdir(parents=True, exist_ok=True)
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:cov", kid="k1", public_key_pem=pub_pem)
    store = LineageStore(lineage_root)
    recorder = SignedLineageLog(store=store, operator_hmac_key=b"0" * 64)
    anchor_coverage_record(
        recorder,
        tool_name="grep",
        tool_call_id=call_id,
        coverage=record,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv_pem,
    )
    return store


# 1 ---------------------------------------------------------------------------
def test_absence_claim_without_coverage_record_reads_unverified(tmp_path: Path) -> None:
    """A tool call that recorded no coverage at all cannot back an absence claim."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _write_tool_call(workdir, "tc-1", coverage=None)

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-1", workdir=workdir)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "no coverage record" in detail.lower()


# 2 ---------------------------------------------------------------------------
def test_unanchored_coverage_record_reads_unverified(tmp_path: Path) -> None:
    """A coverage payload nobody sealed into lineage is not a verified absence."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _write_tool_call(workdir, "tc-2", coverage=_complete_record().to_dict())

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-2", workdir=workdir)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "not anchored" in detail.lower()


# 3 ---------------------------------------------------------------------------
def test_coverage_payload_edited_after_anchoring_reads_unverified(tmp_path: Path) -> None:
    """LOAD-BEARING: a truncated walk relabelled 'complete' after sealing is refused.

    The anchored entry commits to ``sha256`` of the record that was actually
    sealed; a payload rewritten afterwards no longer matches it, so the claim
    degrades instead of reading as a verified absence.
    """
    workdir = tmp_path / "wd"
    workdir.mkdir()
    truncated = ToolCoverageRecord(
        file_count=1,
        corpus_digest=compute_corpus_digest(["a.py"]),
        coverage="partial",
        truncated=True,
        truncation_reason="timeout",
        exit_status="timeout",
        exit_checked=True,
    )
    _anchor(workdir, "tc-3", truncated)
    # The record that reaches the janitor claims a complete walk.
    _write_tool_call(workdir, "tc-3", coverage=_complete_record().to_dict())

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-3", workdir=workdir)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "does not match" in detail.lower()


# 4 ---------------------------------------------------------------------------
def test_coverage_anchored_to_another_tool_call_cannot_back_this_claim(tmp_path: Path) -> None:
    """Scope cannot be borrowed: another call's complete coverage verifies nothing here."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _anchor(workdir, "tc-other", _complete_record())
    _write_tool_call(workdir, "tc-other", coverage=_complete_record().to_dict())
    _write_tool_call(workdir, "tc-4", coverage=_complete_record().to_dict())

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-4", workdir=workdir)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "not anchored" in detail.lower()


# 5 ---------------------------------------------------------------------------
def test_truncated_anchored_coverage_reads_unverified(tmp_path: Path) -> None:
    """An honestly-recorded truncated walk still cannot assert a verified absence."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    truncated = ToolCoverageRecord(
        file_count=3,
        corpus_digest=compute_corpus_digest(["a.py"]),
        coverage="partial",
        truncated=True,
        truncation_reason="limit_reached",
        exit_status=0,
        exit_checked=True,
    )
    _anchor(workdir, "tc-5", truncated)
    _write_tool_call(workdir, "tc-5", coverage=truncated.to_dict())

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-5", workdir=workdir)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "limit_reached" in detail


# 6 ---------------------------------------------------------------------------
def test_unchecked_exit_status_reads_unverified(tmp_path: Path) -> None:
    """A walk whose exit status was never checked is the #3573 shape and is refused."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    unchecked = ToolCoverageRecord(
        file_count=2,
        corpus_digest=compute_corpus_digest(["a.py", "b.py"]),
        coverage="complete",
        truncated=False,
        truncation_reason=None,
        exit_status=0,
        exit_checked=False,
    )
    _anchor(workdir, "tc-6", unchecked)
    _write_tool_call(workdir, "tc-6", coverage=unchecked.to_dict())

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-6", workdir=workdir)
    assert passed is False
    assert "unverified" in detail.lower()
    assert "exit status" in detail.lower()


# 7 ---------------------------------------------------------------------------
def test_complete_anchored_coverage_reads_verified(tmp_path: Path) -> None:
    """A complete walk whose payload matches its anchor is a verified absence."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    record = _complete_record()
    _anchor(workdir, "tc-7", record)
    _write_tool_call(workdir, "tc-7", coverage=record.to_dict())

    passed, detail = verify_anchored_absence_claim(tool_call_id="tc-7", workdir=workdir)
    assert passed is True
    assert "verified absence" in detail.lower()
    assert record.corpus_digest in detail

    # The anchored digest really is sha256 over the canonical payload bytes.
    canonical = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert "sha256:" + hashlib.sha256(canonical).hexdigest() in detail


# 8 ---------------------------------------------------------------------------
def test_absence_verified_signal_dispatches_through_evaluate_signal(tmp_path: Path) -> None:
    """The signal is a first-class completion signal, not an unknown type."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    record = _complete_record()
    _anchor(workdir, "tc-8", record)
    _write_tool_call(workdir, "tc-8", coverage=record.to_dict())

    assert _narrow_signal_type("absence_verified") == "absence_verified"

    signal = CompletionSignal(type="absence_verified", value="tc-8")
    passed, detail = evaluate_signal(signal, workdir)
    assert passed is True
    assert "verified absence" in detail.lower()

    missing = CompletionSignal(type="absence_verified", value="tc-nope")
    passed_missing, detail_missing = evaluate_signal(missing, workdir)
    assert passed_missing is False
    assert "unverified" in detail_missing.lower()
