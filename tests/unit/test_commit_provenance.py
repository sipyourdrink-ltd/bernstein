"""Tests for commit provenance tracking.

All git/subprocess calls are mocked -- no real git operations.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.git.commit_provenance import (
    ProvenanceChain,
    ProvenanceRecord,
    append_provenance_record,
    build_provenance_chain,
    create_provenance_record,
    generate_provenance_attestation,
    render_provenance_report,
    sign_commit_ssh,
    verify_commit_signature,
)
from bernstein.core.git.git_basic import GitResult
from bernstein.core.tasks.models import AgentSession, ModelConfig

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

_SHA = "a" * 40
_SHA2 = "b" * 40
_RUN_ID = "run-001"

_MOD = "bernstein.core.git.commit_provenance"


_SENTINEL_TASKS: list[str] = ["task-42"]


def _session(
    *,
    agent_id: str = "agent-1",
    role: str = "backend",
    task_ids: list[str] | None = None,
    model: str = "sonnet",
) -> AgentSession:
    return AgentSession(
        id=agent_id,
        role=role,
        task_ids=_SENTINEL_TASKS if task_ids is None else task_ids,
        model_config=ModelConfig(model=model, effort="high"),
    )


def _record(
    *,
    sha: str = _SHA,
    agent_id: str = "agent-1",
    task_id: str = "task-42",
    run_id: str = _RUN_ID,
    model: str = "sonnet",
    role: str = "backend",
    ts: float = 1000.0,
    sig: str = "none",
) -> ProvenanceRecord:
    return ProvenanceRecord(
        commit_sha=sha,
        agent_id=agent_id,
        task_id=task_id,
        run_id=run_id,
        model=model,
        role=role,
        timestamp=ts,
        signature_type=sig,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------
# ProvenanceRecord / ProvenanceChain dataclass tests
# ------------------------------------------------------------------


class TestProvenanceRecord:
    def test_frozen(self) -> None:
        rec = _record()
        with pytest.raises(AttributeError):
            rec.commit_sha = "x"  # type: ignore[misc]

    def test_fields_stored(self) -> None:
        rec = _record(sha=_SHA, agent_id="ag-2", task_id="t-1", model="opus")
        assert rec.commit_sha == _SHA
        assert rec.agent_id == "ag-2"
        assert rec.task_id == "t-1"
        assert rec.model == "opus"
        assert rec.signature_type == "none"


class TestProvenanceChain:
    def test_frozen(self) -> None:
        chain = ProvenanceChain(records=(), run_id=_RUN_ID, verified=False)
        with pytest.raises(AttributeError):
            chain.verified = True  # type: ignore[misc]

    def test_empty_chain(self) -> None:
        chain = ProvenanceChain(records=(), run_id=_RUN_ID, verified=False)
        assert len(chain.records) == 0
        assert chain.run_id == _RUN_ID

    def test_chain_with_records(self) -> None:
        r1 = _record(sha=_SHA, ts=1.0)
        r2 = _record(sha=_SHA2, ts=2.0)
        chain = ProvenanceChain(records=(r1, r2), run_id=_RUN_ID, verified=True)
        assert len(chain.records) == 2
        assert chain.verified is True


# ------------------------------------------------------------------
# create_provenance_record
# ------------------------------------------------------------------


class TestCreateProvenanceRecord:
    def test_basic_creation(self) -> None:
        session = _session()
        rec = create_provenance_record(_SHA, session, _RUN_ID)
        assert rec.commit_sha == _SHA
        assert rec.agent_id == "agent-1"
        assert rec.task_id == "task-42"
        assert rec.run_id == _RUN_ID
        assert rec.model == "sonnet"
        assert rec.role == "backend"
        assert rec.signature_type == "none"

    def test_empty_task_ids(self) -> None:
        session = _session(task_ids=[])
        rec = create_provenance_record(_SHA, session, _RUN_ID)
        assert rec.task_id == ""

    def test_multiple_task_ids_uses_first(self) -> None:
        session = _session(task_ids=["t-1", "t-2", "t-3"])
        rec = create_provenance_record(_SHA, session, _RUN_ID)
        assert rec.task_id == "t-1"

    def test_timestamp_is_recent(self) -> None:
        before = time.time()
        rec = create_provenance_record(_SHA, _session(), _RUN_ID)
        after = time.time()
        assert before <= rec.timestamp <= after


# ------------------------------------------------------------------
# sign_commit_ssh
# ------------------------------------------------------------------


class TestSignCommitSsh:
    def test_successful_sign(self, tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("fake-key")
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.side_effect = [
                GitResult(0, _SHA, ""),  # rev-parse HEAD
                GitResult(0, "", ""),  # config gpg.format
                GitResult(0, "", ""),  # config user.signingkey
                GitResult(0, "", ""),  # commit --amend -S
            ]
            assert sign_commit_ssh(_SHA, key, workdir=tmp_path) is True
        assert mock_git.call_count == 4

    def test_head_mismatch(self, tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("fake-key")
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(0, "deadbeef" + "0" * 32, "")
            assert sign_commit_ssh(_SHA, key, workdir=tmp_path) is False
        assert mock_git.call_count == 1

    def test_config_failure(self, tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("fake-key")
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.side_effect = [
                GitResult(0, _SHA, ""),  # rev-parse HEAD
                GitResult(1, "", "err"),  # config gpg.format fails
            ]
            assert sign_commit_ssh(_SHA, key, workdir=tmp_path) is False

    def test_amend_failure(self, tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("fake-key")
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.side_effect = [
                GitResult(0, _SHA, ""),  # rev-parse HEAD
                GitResult(0, "", ""),  # config gpg.format
                GitResult(0, "", ""),  # config user.signingkey
                GitResult(1, "", "sign failed"),  # commit --amend -S
            ]
            assert sign_commit_ssh(_SHA, key, workdir=tmp_path) is False

    def test_rev_parse_fails(self, tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("fake-key")
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(1, "", "not a repo")
            assert sign_commit_ssh(_SHA, key, workdir=tmp_path) is False


# ------------------------------------------------------------------
# verify_commit_signature
# ------------------------------------------------------------------


class TestVerifyCommitSignature:
    def test_good_ssh_signature(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(0, f'Good "git" signature for user@host\n{_SHA}', "")
            assert verify_commit_signature(_SHA, tmp_path) is True

    def test_good_gpg_signature(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(0, "", 'gpg: Good signature from "User <u@x.com>"')
            assert verify_commit_signature(_SHA, tmp_path) is True

    def test_no_signature(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(0, _SHA, "")
            assert verify_commit_signature(_SHA, tmp_path) is False

    def test_git_log_fails(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(128, "", "fatal: bad object")
            assert verify_commit_signature(_SHA, tmp_path) is False

    def test_good_signature_from_stdout(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}.run_git") as mock_git:
            mock_git.return_value = GitResult(0, 'Good signature from "Bernstein Agent"', "")
            assert verify_commit_signature(_SHA, tmp_path) is True


# ------------------------------------------------------------------
# build_provenance_chain
# ------------------------------------------------------------------


class TestBuildProvenanceChain:
    def test_missing_archive(self, tmp_path: Path) -> None:
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        assert chain.records == ()
        assert chain.run_id == _RUN_ID
        assert chain.verified is False

    def test_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / "provenance.jsonl").write_text("")
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        assert chain.records == ()

    def test_single_record(self, tmp_path: Path) -> None:
        entry = {
            "commit_sha": _SHA,
            "agent_id": "agent-1",
            "task_id": "task-42",
            "run_id": _RUN_ID,
            "model": "sonnet",
            "role": "backend",
            "timestamp": 1000.0,
            "signature_type": "ssh",
        }
        (tmp_path / "provenance.jsonl").write_text(json.dumps(entry) + "\n")
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        assert len(chain.records) == 1
        assert chain.records[0].commit_sha == _SHA
        assert chain.records[0].signature_type == "ssh"

    def test_filters_by_run_id(self, tmp_path: Path) -> None:
        e1 = {
            "commit_sha": _SHA,
            "agent_id": "a1",
            "task_id": "t1",
            "run_id": _RUN_ID,
            "model": "sonnet",
            "role": "backend",
            "timestamp": 1.0,
            "signature_type": "none",
        }
        e2 = {
            "commit_sha": _SHA2,
            "agent_id": "a2",
            "task_id": "t2",
            "run_id": "other-run",
            "model": "opus",
            "role": "qa",
            "timestamp": 2.0,
            "signature_type": "gpg",
        }
        content = json.dumps(e1) + "\n" + json.dumps(e2) + "\n"
        (tmp_path / "provenance.jsonl").write_text(content)
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        assert len(chain.records) == 1
        assert chain.records[0].agent_id == "a1"

    def test_records_sorted_by_timestamp(self, tmp_path: Path) -> None:
        entries = []
        for i, ts in enumerate([3.0, 1.0, 2.0]):
            entries.append(
                {
                    "commit_sha": f"{'0' * 39}{i}",
                    "agent_id": f"a{i}",
                    "task_id": f"t{i}",
                    "run_id": _RUN_ID,
                    "model": "sonnet",
                    "role": "backend",
                    "timestamp": ts,
                    "signature_type": "none",
                }
            )
        lines = "\n".join(json.dumps(e) for e in entries) + "\n"
        (tmp_path / "provenance.jsonl").write_text(lines)
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        timestamps = [r.timestamp for r in chain.records]
        assert timestamps == sorted(timestamps)

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        entry = {
            "commit_sha": _SHA,
            "agent_id": "a1",
            "task_id": "t1",
            "run_id": _RUN_ID,
            "model": "sonnet",
            "role": "backend",
            "timestamp": 1.0,
            "signature_type": "none",
        }
        content = "\n\n" + json.dumps(entry) + "\n\n"
        (tmp_path / "provenance.jsonl").write_text(content)
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        assert len(chain.records) == 1


# ------------------------------------------------------------------
# generate_provenance_attestation
# ------------------------------------------------------------------


class TestGenerateProvenanceAttestation:
    def test_empty_chain(self) -> None:
        chain = ProvenanceChain(records=(), run_id=_RUN_ID, verified=False)
        att = generate_provenance_attestation(chain)
        assert att["_type"] == "https://in-toto.io/Statement/v1"
        assert att["subject"] == []
        assert att["predicate"]["metadata"]["record_count"] == 0

    def test_single_record_attestation(self) -> None:
        rec = _record()
        chain = ProvenanceChain(records=(rec,), run_id=_RUN_ID, verified=True)
        att = generate_provenance_attestation(chain)
        assert att["predicateType"] == "https://bernstein.dev/provenance/v1"
        assert len(att["subject"]) == 1
        assert att["subject"][0]["digest"]["gitCommit"] == _SHA
        assert att["predicate"]["metadata"]["verified"] is True
        assert att["predicate"]["builder"]["id"] == "bernstein-orchestrator"

    def test_multiple_records(self) -> None:
        r1 = _record(sha=_SHA, ts=1.0)
        r2 = _record(sha=_SHA2, ts=2.0, agent_id="agent-2")
        chain = ProvenanceChain(records=(r1, r2), run_id=_RUN_ID, verified=False)
        att = generate_provenance_attestation(chain)
        assert len(att["subject"]) == 2
        assert len(att["predicate"]["materials"]) == 2
        assert att["predicate"]["materials"][1]["agent_id"] == "agent-2"

    def test_attestation_is_json_serialisable(self) -> None:
        rec = _record()
        chain = ProvenanceChain(records=(rec,), run_id=_RUN_ID, verified=False)
        att = generate_provenance_attestation(chain)
        serialised = json.dumps(att)
        assert json.loads(serialised) == att


# ------------------------------------------------------------------
# render_provenance_report
# ------------------------------------------------------------------


class TestRenderProvenanceReport:
    def test_empty_chain_report(self) -> None:
        chain = ProvenanceChain(records=(), run_id=_RUN_ID, verified=False)
        report = render_provenance_report(chain)
        assert _RUN_ID in report
        assert "No provenance records found" in report
        assert "Verified:** no" in report

    def test_single_record_table(self) -> None:
        rec = _record(sha=_SHA, agent_id="agent-1", task_id="task-42", model="sonnet")
        chain = ProvenanceChain(records=(rec,), run_id=_RUN_ID, verified=True)
        report = render_provenance_report(chain)
        assert "Verified:** yes" in report
        assert _SHA[:8] in report
        assert "agent-1" in report
        assert "task-42" in report
        assert "sonnet" in report
        assert "| Commit |" in report

    def test_multiple_rows(self) -> None:
        r1 = _record(sha=_SHA, agent_id="a1")
        r2 = _record(sha=_SHA2, agent_id="a2")
        chain = ProvenanceChain(records=(r1, r2), run_id=_RUN_ID, verified=False)
        report = render_provenance_report(chain)
        assert report.count("| `") == 2

    def test_report_contains_header_row(self) -> None:
        rec = _record()
        chain = ProvenanceChain(records=(rec,), run_id=_RUN_ID, verified=False)
        report = render_provenance_report(chain)
        assert "| Commit | Agent | Task | Model | Role | Signature |" in report
        assert "|--------|-------|------|-------|------|-----------|" in report


# ------------------------------------------------------------------
# append_provenance_record
# ------------------------------------------------------------------


class TestAppendProvenanceRecord:
    def test_creates_directory_and_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "sub" / "archive"
        rec = _record()
        append_provenance_record(rec, dest)
        jsonl = dest / "provenance.jsonl"
        assert jsonl.exists()
        data = json.loads(jsonl.read_text().strip())
        assert data["commit_sha"] == _SHA

    def test_appends_multiple_records(self, tmp_path: Path) -> None:
        r1 = _record(sha=_SHA)
        r2 = _record(sha=_SHA2)
        append_provenance_record(r1, tmp_path)
        append_provenance_record(r2, tmp_path)
        lines = (tmp_path / "provenance.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["commit_sha"] == _SHA
        assert json.loads(lines[1])["commit_sha"] == _SHA2

    def test_roundtrip_append_then_build(self, tmp_path: Path) -> None:
        rec = _record(sha=_SHA, run_id=_RUN_ID)
        append_provenance_record(rec, tmp_path)
        chain = build_provenance_chain(_RUN_ID, tmp_path)
        assert len(chain.records) == 1
        assert chain.records[0].commit_sha == _SHA
