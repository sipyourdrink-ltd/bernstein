"""Real-process and storage witnesses for the cross-tier contract (#6559)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.verification import run_verify, verify_evidence_hash
from bernstein.core.verification.tiers import EdgeVerifier, ResearchVerifier, SandboxVerifier, SearchVerifier


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("tier", ["edge", "sandbox"])
@pytest.mark.parametrize(
    "content,passed", [("VALUE = 1  # valid\n", True), ("VALUE = (\n", False), ("VALUE = 2\n", False)]
)
def test_returned_diff_is_actually_checked(candidate, tier, content, passed):
    repo, make = candidate
    result = make(content)
    verifier = EdgeVerifier(test_paths=["test_value.py"])
    if tier == "sandbox":
        verifier = SandboxVerifier()
        result["verify_command"] = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_value.py"]
    record = run_verify(verifier, result, sleep_fn=lambda _: None)
    assert record.ok is passed
    assert (repo / "value.py").read_text() == "VALUE = 1\n"
    assert record.final_verdict.evidence["base_commit"] == result["base_commit"]
    assert record.final_verdict.evidence["diff_hash"] == "sha256:" + hashlib.sha256(result["diff"].encode()).hexdigest()


def test_missing_base_and_preapplied_diff_fail(candidate):
    repo, make = candidate
    result = make("VALUE = 2\n")
    result.pop("base_commit")
    assert not EdgeVerifier().verify(result).ok
    result = make("VALUE = 2\n")
    result["pre_applied"] = True
    assert not EdgeVerifier().verify(result).ok
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("content,passed", [("original", True), ("modified", False)])
def test_search_compares_trusted_expected_digest(content, passed):
    verifier = SearchVerifier(
        expected_digests={"https://example.test": hashlib.sha256(b"original").hexdigest()}, manifest_id="capture-123"
    )
    result = verifier.verify({"results": [{"url": "https://example.test", "content": content}]})
    assert result.ok is passed
    assert result.evidence["manifest_id"] == "capture-123"


def test_search_missing_expectations_fails():
    assert not SearchVerifier().verify({"results": [{"url": "https://example.test", "content": "text"}]}).ok


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1, 0.3])
def test_research_invalid_and_low_scores_fail(score):
    result = run_verify(
        ResearchVerifier(judge_fn=lambda *_: score, judge_id="judge-test"),
        {"report": "Report"},
        sleep_fn=lambda _: None,
    )
    assert not result.ok


def test_research_requires_judge():
    assert not ResearchVerifier().verify({"report": "Report"}).ok


def test_real_stores_retain_verifiable_evidence(tmp_path):
    spine = LineageSpine(tmp_path / "lineage", run_id="run-123", hmac_key=b"test-key")
    chain = AuditChainStore(tmp_path / "audit", key=b"test-key")
    verifier = ResearchVerifier(judge_fn=lambda *_: 0.9, judge_id="judge-test")
    record = run_verify(
        verifier, {"report": "Report", "backend": "research-backend"}, run_id="run-123", spine=spine, audit_chain=chain
    )
    assert record.ok and record.recorded
    entries = list(spine.iter_entries())
    assert len(entries) == 1 and spine.verify().ok
    artifact = spine.run_dir / entries[0].artifact_path
    payload = json.loads(artifact.read_text())
    assert entries[0].content_hash == "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert payload["run_id"] == "run-123"
    assert payload["final_verdict"]["backend"] == "research-backend"
    assert verify_evidence_hash(payload["final_verdict"]["evidence"]) == record.final_verdict.evidence_hash
    events = chain.query(event_type="verify_outcome", resource_id="run-123")
    assert len(events) == 1 and chain.verify() == (True, [])
    assert events[0].details["evidence_hash"] == record.final_verdict.evidence_hash
    assert events[0].details["spine_entry_hash"] == entries[0].entry_hash


@pytest.mark.parametrize("stage", ["artifact", "lineage", "audit"])
def test_recording_failure_is_explicit(tmp_path, monkeypatch, stage):
    from bernstein.core.verification import VerificationRecordingError

    spine = LineageSpine(tmp_path / "lineage", run_id="run-123", hmac_key=b"key")
    chain = AuditChainStore(tmp_path / "audit", key=b"key")
    if stage == "artifact":
        spine.run_dir.mkdir(parents=True)
        (spine.run_dir / "verification").write_text("blocks directory creation")
    else:

        def fail(**kwargs):
            raise OSError("disk failure")

        monkeypatch.setattr(
            spine if stage == "lineage" else chain, "record" if stage == "lineage" else "log_with_prev_digest", fail
        )
    with pytest.raises(VerificationRecordingError) as error:
        run_verify(
            ResearchVerifier(judge_fn=lambda *_: 0.9, judge_id="test"),
            {"report": "Report"},
            run_id="run-123",
            spine=spine,
            audit_chain=chain,
        )
    assert not error.value.record.recorded
    if stage == "audit":
        assert error.value.record.spine_entry_hash
        assert spine.verify().ok
    assert not chain.query()


def test_wrong_run_and_incomplete_storage_rejected(tmp_path):
    spine = LineageSpine(tmp_path / "lineage", run_id="one", hmac_key=b"key")
    chain = AuditChainStore(tmp_path / "audit", key=b"key")
    with pytest.raises(ValueError, match="run_id"):
        run_verify(ResearchVerifier(), {}, run_id="two", spine=spine, audit_chain=chain)
    with pytest.raises(ValueError, match="both"):
        run_verify(ResearchVerifier(), {}, run_id="one", spine=spine)
    assert not list(spine.iter_entries())
    assert not chain.query()


def test_source_name_containing_test_is_not_a_test(candidate):
    _, make = candidate
    # Rename the tracked source through the patch, so its basename contains
    # 'test' but does not match a pytest module naming convention.
    result = make("VALUE = 1  # checked\n")
    result["diff"] = (
        "diff --git a/latest_cache.py b/latest_cache.py\nnew file mode 100644\n--- /dev/null\n+++ b/latest_cache.py\n@@ -0,0 +1 @@\n+VALUE = 1\n"
    )
    verdict = EdgeVerifier().verify(result)
    assert verdict.ok
    assert verdict.evidence["pytest_status"] == "not_selected"


def test_empty_pytest_collection_fails(candidate):
    _, make = candidate
    result = make("VALUE = 1  # no tests\n", "test_value.py")
    verdict = EdgeVerifier().verify(result)
    assert not verdict.ok
    assert verdict.evidence["pytest_exit_code"] == 5
    assert verdict.evidence["pytest_status"] == "no_tests_collected"


@pytest.mark.asyncio
@pytest.mark.parametrize("score,passed", [(0.9, True), (0.3, False)])
async def test_janitor_executes_and_records_cross_tier_gate(tmp_path, score, passed):
    from bernstein.core.models import CompletionSignal, Task, TaskType

    from bernstein.core.quality.janitor import run_janitor
    from bernstein.core.verification import VerificationRequest

    task = Task(
        id="T-123",
        title="Report",
        description="Report",
        role="analyst",
        task_type=TaskType.RESEARCH,
        completion_signals=[CompletionSignal(type="path_exists", value="report.txt")],
    )
    (tmp_path / "report.txt").write_text("Report")
    spine = LineageSpine(tmp_path / "lineage", run_id=task.id, hmac_key=b"key")
    chain = AuditChainStore(tmp_path / "audit", key=b"key")
    request = VerificationRequest(
        ResearchVerifier(judge_fn=lambda *_: score, judge_id="test"), {"report": "Report"}, spine, chain
    )
    results = await run_janitor([task], tmp_path, tier_verifications={task.id: request})
    assert results[0].passed is passed
    assert any(name == "cross_tier:verification" and ok is passed for name, ok, _ in results[0].signal_results)
    assert len(list(spine.iter_entries())) == 1 and spine.verify().ok
    assert len(chain.query()) == 1 and chain.verify() == (True, [])


def test_canonical_hash_matches_ladder_unicode_and_nested_values():
    from bernstein.core.quality.verifier_ladder import canonical_hash

    evidence = {"text": "Привет שלום", "nested": {"z": 1.0, "a": [True, None]}}
    assert verify_evidence_hash(evidence) == canonical_hash(evidence)
    with pytest.raises(ValueError):
        verify_evidence_hash({"invalid": float("nan")})


@pytest.mark.parametrize("tier", ["builtin", "custom"])
def test_retry_exhaustion_preserves_actual_history(tmp_path, tier):
    from bernstein.core.verification import VerifyContract, VerifyVerdict
    from bernstein.core.verification.contract import Verdict

    class AlwaysRetry(VerifyContract):
        @property
        def tier(self):
            return "search"

        def verify(self, task_result, *, attempt=1):
            return VerifyVerdict(Verdict.RETRY, self.tier, "test", evidence={"attempt": attempt})

        def max_retries(self):
            return 1

    verifier = SearchVerifier() if tier == "builtin" else AlwaysRetry()
    spine = LineageSpine(tmp_path / "lineage", run_id="retry", hmac_key=b"key")
    chain = AuditChainStore(tmp_path / "audit", key=b"key")
    record = run_verify(
        verifier,
        {"results": [{"url": "url", "content": ""}]},
        run_id="retry",
        spine=spine,
        audit_chain=chain,
        sleep_fn=lambda _: None,
    )
    assert record.recorded and not record.ok
    assert [v.verdict for v in record.attempts] == [Verdict.RETRY, Verdict.RETRY]
    assert [v.attempt for v in record.attempts] == [1, 2]
    assert record.final_verdict.verdict is Verdict.FAIL
    saved = json.loads(Path(record.artifact_path).read_text())
    assert [v["verdict"] for v in saved["attempts"]] == ["retry", "retry"]
    assert saved["final_verdict"]["verdict"] == "fail"


@pytest.mark.asyncio
async def test_janitor_cannot_accept_a_recording_failure(tmp_path):
    from bernstein.core.models import Task, TaskType

    from bernstein.core.quality.janitor import run_janitor
    from bernstein.core.verification import VerificationRequest

    task = Task(id="T-123", title="Report", description="Report", role="analyst", task_type=TaskType.RESEARCH)
    spine = LineageSpine(tmp_path / "lineage", run_id=task.id, hmac_key=b"key")
    spine.run_dir.mkdir(parents=True)
    (spine.run_dir / "verification").write_text("not a directory")
    chain = AuditChainStore(tmp_path / "audit", key=b"key")
    request = VerificationRequest(
        ResearchVerifier(judge_fn=lambda *_: 0.9, judge_id="test"), {"report": "Report"}, spine, chain
    )
    results = await run_janitor([task], tmp_path, tier_verifications={task.id: request})
    assert not results[0].passed
    assert results[0].signal_results == [("cross_tier:verification", False, results[0].signal_results[0][2])]
    assert "recording incomplete" in results[0].signal_results[0][2]


def test_sandbox_missing_executable_is_terminal(candidate):
    _, make = candidate
    result = make("VALUE = 1  # checked\n")
    result["verify_command"] = ["/no-such-bernstein-test-executable"]
    assert SandboxVerifier().verify(result).verdict.value == "fail"


def test_search_cannot_trust_backend_supplied_expectations():
    digest = hashlib.sha256(b"untrusted").hexdigest()
    result = {
        "results": [{"url": "url", "content": "untrusted"}],
        "expected_digests": {"url": digest},
        "manifest_id": "forged",
    }
    assert not SearchVerifier().verify(result).ok
