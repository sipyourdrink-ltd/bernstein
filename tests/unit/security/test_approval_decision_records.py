"""Approval gates honour only authenticated decision records.

Both file-based task approval gates -- the pre-spawn gate
(:func:`bernstein.core.orchestration.approval_gate.wait_for_approval`, "G1")
and the post-completion review gate
(:meth:`bernstein.core.security.approval.ApprovalGate.evaluate`, "G2") --
read ``<task_id>.approved`` / ``<task_id>.rejected`` under
``.sdd/runtime/approvals/``. These tests pin the contract:

* a decision made through ``bernstein approve`` / ``reject`` or the task
  server's approval routes is honoured and attributed to that path and its
  principal;
* anything else in a decision slot (a plain file, a record for another task,
  a record answering an earlier request) resolves to rejected with the
  decision source ``unverified-file``, and the audit chain says so;
* with no decision file, the timeout policy is unchanged.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import ApprovalSpec, Complexity, Scope, Task, TaskStatus, TaskType
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.cli.commands.approve_cmd import approve
from bernstein.cli.commands.reject_cmd import reject
from bernstein.core.approval.models import local_shell_principal
from bernstein.core.orchestration.approval_gate import wait_for_approval
from bernstein.core.routes import approvals as approval_routes
from bernstein.core.security.approval import ApprovalGate, ApprovalMode
from bernstein.core.security.approval_decision import build_decision_record, write_decision_record
from bernstein.core.security.audit import AuditLog
from bernstein.core.tasks.lifecycle import set_audit_log

_APPROVALS = Path(".sdd") / "runtime" / "approvals"
_REVIEW_PENDING = Path(".sdd") / "runtime" / "pending_approvals"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_log(tmp_path: Path) -> Iterator[AuditLog]:
    log = AuditLog(tmp_path / "audit", key=b"k" * 32)
    set_audit_log(log)
    try:
        yield log
    finally:
        import bernstein.core.tasks.lifecycle as _lifecycle

        _lifecycle._audit_log = None


def _events(tmp_path: Path, event_type: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for log_file in sorted((tmp_path / "audit").glob("*.jsonl")):
        for raw in log_file.read_text().splitlines():
            if raw.strip():
                row = json.loads(raw)
                if row.get("event_type") == event_type:
                    rows.append(row)
    return rows


def _resolved_details(tmp_path: Path) -> dict[str, Any]:
    rows = _events(tmp_path, "approval_resolved")
    assert rows, "no approval_resolved audit event"
    details = rows[-1]["details"]
    assert isinstance(details, dict)
    return details


def _g1_nonce(tmp_path: Path, task_id: str) -> str:
    data = json.loads((tmp_path / _APPROVALS / f"{task_id}.pending").read_text(encoding="utf-8"))
    nonce = data["nonce"]
    assert isinstance(nonce, str) and nonce
    return nonce


def _run_g1(
    tmp_path: Path,
    task_id: str,
    audit_log: AuditLog,
    *,
    during_wait: Callable[[], None] | None = None,
    spec: ApprovalSpec | None = None,
) -> str:
    """Run the pre-spawn gate; *during_wait* runs on the first poll sleep."""
    calls = {"n": 0}

    def _sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1 and during_wait is not None:
            during_wait()

    clock = itertools.count(0.0, 1.0)
    return wait_for_approval(
        task_id,
        spec or ApprovalSpec(prompt="ship?", timeout_seconds=30),
        workdir=tmp_path,
        audit_log=audit_log,
        monotonic=lambda: next(clock),
        sleep=_sleep,
    )


def _signed(task_id: str, outcome: str, nonce: str, *, source: str = "cli") -> dict[str, Any]:
    return build_decision_record(
        task_id=task_id,
        outcome=outcome,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        principal=local_shell_principal().to_dict(),
        nonce=nonce,
    )


def _make_task(task_id: str) -> Task:
    return Task(
        id=task_id,
        title="Add auth",
        description="Implement auth.",
        role="backend",
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.DONE,
        task_type=TaskType.STANDARD,
    )


def _wait_for_review_nonce(tmp_path: Path, task_id: str, *, not_equal: str = "") -> str:
    path = tmp_path / _REVIEW_PENDING / f"{task_id}.json"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            nonce = json.loads(path.read_text(encoding="utf-8")).get("nonce", "")
        except (OSError, ValueError):
            nonce = ""
        if nonce and nonce != not_equal:
            return str(nonce)
        time.sleep(0.01)
    raise AssertionError("review gate never published a request nonce")


def _run_g2(
    tmp_path: Path,
    task_id: str,
    *,
    during_wait: Callable[[str], None] | None = None,
    previous_nonce: str = "",
    timeout_s: float = 5.0,
) -> Any:
    """Run the review gate; *during_wait(nonce)* runs once the request is open."""
    gate = ApprovalGate(ApprovalMode.REVIEW, tmp_path, _poll_interval_s=0.01)
    errors: list[BaseException] = []

    def _actor() -> None:
        try:
            nonce = _wait_for_review_nonce(tmp_path, task_id, not_equal=previous_nonce)
            assert during_wait is not None
            during_wait(nonce)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=_actor) if during_wait is not None else None
    if thread is not None:
        thread.start()
    result = gate.evaluate(_make_task(task_id), session_id="sess-1", timeout_s=timeout_s)
    if thread is not None:
        thread.join(timeout=5)
    assert not errors, errors
    return result


def _web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(approval_routes.router)
    monkeypatch.setattr(approval_routes, "_PENDING_DIR", tmp_path / _REVIEW_PENDING)
    monkeypatch.setattr(approval_routes, "_APPROVALS_DIR", tmp_path / _APPROVALS)
    return TestClient(app)


# ---------------------------------------------------------------------------
# G1: pre-spawn gate
# ---------------------------------------------------------------------------


class TestPreSpawnGate:
    def test_plain_empty_approved_file_is_rejected_as_unverified(self, tmp_path: Path, audit_log: AuditLog) -> None:
        slot = tmp_path / _APPROVALS / "T-plain.approved"

        def _plant() -> None:
            with open(slot, "w", encoding="utf-8"):
                pass

        assert _run_g1(tmp_path, "T-plain", audit_log, during_wait=_plant) == "rejected"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "unverified-file"
        assert details["outcome"] == "rejected"
        assert details["verified"] is False
        assert details["verification_failure"] == "not-a-record"

    def test_plain_file_present_before_the_gate_opens_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        approvals = tmp_path / _APPROVALS
        approvals.mkdir(parents=True)
        (approvals / "T-early.approved").write_text("approved")

        assert _run_g1(tmp_path, "T-early", audit_log) == "rejected"
        assert _resolved_details(tmp_path)["decision_source"] == "unverified-file"
        # The planted file is kept as evidence but no longer occupies the slot.
        assert not (approvals / "T-early.approved").exists()
        assert (approvals / "T-early.approved.unverified").exists()

    def test_plain_rejected_file_is_unverified_too(self, tmp_path: Path, audit_log: AuditLog) -> None:
        slot = tmp_path / _APPROVALS / "T-plain-rej.rejected"
        outcome = _run_g1(tmp_path, "T-plain-rej", audit_log, during_wait=lambda: slot.write_text("rejected"))
        assert outcome == "rejected"
        assert _resolved_details(tmp_path)["decision_source"] == "unverified-file"

    def test_record_signed_for_another_task_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _copy_other_task() -> None:
            nonce = _g1_nonce(tmp_path, "T-B")
            write_decision_record(tmp_path / _APPROVALS / "T-B.approved", _signed("T-A", "approved", nonce))

        assert _run_g1(tmp_path, "T-B", audit_log, during_wait=_copy_other_task) == "rejected"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "unverified-file"
        assert details["verification_failure"] == "task-mismatch"

    def test_record_from_an_earlier_request_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        slot = tmp_path / _APPROVALS / "T-replay.approved"
        first: dict[str, bytes] = {}

        def _operator_approves() -> None:
            result = CliRunner().invoke(approve, ["T-replay", "--workdir", str(tmp_path), "--no-prompt"])
            assert result.exit_code == 0, result.output
            first["bytes"] = slot.read_bytes()

        assert _run_g1(tmp_path, "T-replay", audit_log, during_wait=_operator_approves) == "approved"
        old_record = first["bytes"]

        def _replay() -> None:
            slot.write_bytes(old_record)

        assert _run_g1(tmp_path, "T-replay", audit_log, during_wait=_replay) == "rejected"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "unverified-file"
        assert details["verification_failure"] == "nonce-mismatch"

    def test_genuine_record_left_by_an_earlier_request_is_superseded_not_honoured(
        self, tmp_path: Path, audit_log: AuditLog
    ) -> None:
        approvals = tmp_path / _APPROVALS
        approvals.mkdir(parents=True)
        write_decision_record(approvals / "T-old.approved", _signed("T-old", "approved", "0" * 32))

        spec = ApprovalSpec(prompt="ship?", timeout_seconds=3, default_action="reject")
        assert _run_g1(tmp_path, "T-old", audit_log, spec=spec) == "timeout"
        superseded = _events(tmp_path, "approval_decision_superseded")
        assert len(superseded) == 1
        assert superseded[0]["details"]["decision_source"] == "cli"

    def test_bad_signature_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _tamper() -> None:
            record = _signed("T-mac", "approved", _g1_nonce(tmp_path, "T-mac"))
            record["principal"] = {**record["principal"], "identifier": "os-user:someone-else"}
            write_decision_record(tmp_path / _APPROVALS / "T-mac.approved", record)

        assert _run_g1(tmp_path, "T-mac", audit_log, during_wait=_tamper) == "rejected"
        assert _resolved_details(tmp_path)["verification_failure"] == "bad-signature"

    def test_record_in_the_wrong_slot_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _swap() -> None:
            record = _signed("T-slot", "rejected", _g1_nonce(tmp_path, "T-slot"))
            write_decision_record(tmp_path / _APPROVALS / "T-slot.approved", record)

        assert _run_g1(tmp_path, "T-slot", audit_log, during_wait=_swap) == "rejected"
        assert _resolved_details(tmp_path)["verification_failure"] == "outcome-mismatch"

    def test_cli_approve_is_honoured_with_source_and_principal(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _operator() -> None:
            result = CliRunner().invoke(approve, ["T-cli", "--workdir", str(tmp_path), "--no-prompt"])
            assert result.exit_code == 0, result.output

        assert _run_g1(tmp_path, "T-cli", audit_log, during_wait=_operator) == "approved"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "cli"
        assert details["verified"] is True
        assert details["principal"] == local_shell_principal().identifier
        assert details["principal_auth_method"] == "local-shell"

    def test_cli_reject_is_honoured_with_source(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _operator() -> None:
            result = CliRunner().invoke(reject, ["T-cli-rej", "--workdir", str(tmp_path), "--no-prompt"])
            assert result.exit_code == 0, result.output

        assert _run_g1(tmp_path, "T-cli-rej", audit_log, during_wait=_operator) == "rejected"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "cli"
        assert details["verified"] is True

    def test_web_route_approve_is_honoured_with_source_web(
        self, tmp_path: Path, audit_log: AuditLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _web_client(tmp_path, monkeypatch)

        def _operator() -> None:
            resp = client.post("/approvals/T-web/approve", json={"reason": "looks fine"})
            assert resp.status_code == 200, resp.text

        assert _run_g1(tmp_path, "T-web", audit_log, during_wait=_operator) == "approved"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "web"
        assert details["principal"] == "dashboard-operator"
        assert details["principal_auth_method"] == "loopback"

    def test_web_route_reject_is_honoured_with_source_web(
        self, tmp_path: Path, audit_log: AuditLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _web_client(tmp_path, monkeypatch)

        def _operator() -> None:
            resp = client.post("/approvals/T-web-rej/reject", json={"reason": "no"})
            assert resp.status_code == 200, resp.text

        assert _run_g1(tmp_path, "T-web-rej", audit_log, during_wait=_operator) == "rejected"
        assert _resolved_details(tmp_path)["decision_source"] == "web"

    def test_web_route_answers_503_and_writes_nothing_when_the_decision_key_is_unloadable(
        self, tmp_path: Path, audit_log: AuditLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An audit key readable by other users is a hard error at load time.

        The route must surface it as 503 and leave the decision slot empty, so
        the gate falls through to its timeout policy instead of reading an
        unsigned or partial record.
        """
        client = _web_client(tmp_path, monkeypatch)
        key_path = Path(os.environ["BERNSTEIN_AUDIT_KEY_PATH"])
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(b"k" * 64)
        key_path.chmod(0o644)

        def _operator() -> None:
            resp = client.post("/approvals/T-web-503/approve", json={"reason": "looks fine"})
            assert resp.status_code == 503, resp.text
            assert resp.json()["detail"] == "Decision key unavailable"

        assert _run_g1(tmp_path, "T-web-503", audit_log, during_wait=_operator) == "timeout"
        assert not (tmp_path / _APPROVALS / "T-web-503.approved").exists()
        assert _resolved_details(tmp_path)["decision_source"] == "timeout-default"

    def test_no_decision_file_keeps_the_timeout_policy(self, tmp_path: Path, audit_log: AuditLog) -> None:
        spec = ApprovalSpec(prompt="ship?", timeout_seconds=3, default_action="approve")
        assert _run_g1(tmp_path, "T-none", audit_log, spec=spec) == "timeout"
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "timeout-default"
        assert details["applied_outcome"] == "approved"


# ---------------------------------------------------------------------------
# G2: post-completion review gate
# ---------------------------------------------------------------------------


class TestReviewGate:
    def test_plain_empty_approved_file_is_rejected_as_unverified(self, tmp_path: Path, audit_log: AuditLog) -> None:
        slot = tmp_path / _APPROVALS / "T-r-plain.approved"

        def _plant(_nonce: str) -> None:
            with open(slot, "w", encoding="utf-8"):
                pass

        result = _run_g2(tmp_path, "T-r-plain", during_wait=_plant)
        assert result.approved is False
        assert result.rejected is True
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "unverified-file"
        assert details["gate"] == "post-completion-review"

    def test_plain_rejected_file_is_unverified_too(self, tmp_path: Path, audit_log: AuditLog) -> None:
        slot = tmp_path / _APPROVALS / "T-r-prej.rejected"
        result = _run_g2(tmp_path, "T-r-prej", during_wait=lambda _n: slot.write_text("rejected"))
        assert result.rejected is True
        assert _resolved_details(tmp_path)["decision_source"] == "unverified-file"

    def test_record_signed_for_another_task_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _copy(nonce: str) -> None:
            write_decision_record(tmp_path / _APPROVALS / "T-r-B.approved", _signed("T-r-A", "approved", nonce))

        result = _run_g2(tmp_path, "T-r-B", during_wait=_copy)
        assert result.rejected is True
        assert _resolved_details(tmp_path)["verification_failure"] == "task-mismatch"

    def test_record_from_an_earlier_request_is_rejected(self, tmp_path: Path, audit_log: AuditLog) -> None:
        slot = tmp_path / _APPROVALS / "T-r-replay.approved"
        first: dict[str, Any] = {}

        def _operator(nonce: str) -> None:
            first["nonce"] = nonce
            result = CliRunner().invoke(approve, ["T-r-replay", "--workdir", str(tmp_path), "--no-prompt"])
            assert result.exit_code == 0, result.output
            first["bytes"] = slot.read_bytes()

        assert _run_g2(tmp_path, "T-r-replay", during_wait=_operator).approved is True

        result = _run_g2(
            tmp_path,
            "T-r-replay",
            during_wait=lambda _n: slot.write_bytes(first["bytes"]),
            previous_nonce=first["nonce"],
        )
        assert result.rejected is True
        assert _resolved_details(tmp_path)["verification_failure"] == "nonce-mismatch"

    def test_cli_approve_is_honoured_with_source_and_principal(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _operator(_nonce: str) -> None:
            result = CliRunner().invoke(approve, ["T-r-cli", "--workdir", str(tmp_path), "--no-prompt"])
            assert result.exit_code == 0, result.output

        result = _run_g2(tmp_path, "T-r-cli", during_wait=_operator)
        assert result.approved is True
        details = _resolved_details(tmp_path)
        assert details["decision_source"] == "cli"
        assert details["principal"] == local_shell_principal().identifier
        assert details["gate"] == "post-completion-review"

    def test_cli_reject_is_honoured(self, tmp_path: Path, audit_log: AuditLog) -> None:
        def _operator(_nonce: str) -> None:
            result = CliRunner().invoke(reject, ["T-r-crej", "--workdir", str(tmp_path), "--no-prompt"])
            assert result.exit_code == 0, result.output

        result = _run_g2(tmp_path, "T-r-crej", during_wait=_operator)
        assert result.rejected is True
        assert result.resolution == "decided"
        assert _resolved_details(tmp_path)["decision_source"] == "cli"

    def test_web_route_approve_is_honoured_with_source_web(
        self, tmp_path: Path, audit_log: AuditLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _web_client(tmp_path, monkeypatch)

        def _operator(_nonce: str) -> None:
            resp = client.post("/approvals/T-r-web/approve", json={"reason": "ok"})
            assert resp.status_code == 200, resp.text

        assert _run_g2(tmp_path, "T-r-web", during_wait=_operator).approved is True
        assert _resolved_details(tmp_path)["decision_source"] == "web"

    def test_no_decision_file_keeps_the_timeout_policy(self, tmp_path: Path, audit_log: AuditLog) -> None:
        result = _run_g2(tmp_path, "T-r-none", timeout_s=0.05)
        assert result.rejected is True
        assert result.resolution == "timed_out"


# ---------------------------------------------------------------------------
# Parked-task resume (``--until approval``)
# ---------------------------------------------------------------------------


class TestParkedTaskApproval:
    def test_plain_approved_file_does_not_bind_a_resume(self, tmp_path: Path) -> None:
        from bernstein.core.tasks.suspension import approval_decision_ref

        approvals = tmp_path / _APPROVALS
        approvals.mkdir(parents=True)
        (approvals / "T-park.approved").write_text("approved")
        assert approval_decision_ref(tmp_path, "T-park") == ""

    def test_cli_approval_binds_a_resume(self, tmp_path: Path) -> None:
        from bernstein.core.tasks.suspension import approval_decision_ref

        result = CliRunner().invoke(approve, ["T-park-ok", "--workdir", str(tmp_path), "--no-prompt"])
        assert result.exit_code == 0, result.output
        assert "no open approval request" in " ".join(result.output.split())
        assert approval_decision_ref(tmp_path, "T-park-ok")

    def test_record_for_another_task_does_not_bind_a_resume(self, tmp_path: Path) -> None:
        from bernstein.core.tasks.suspension import approval_decision_ref

        write_decision_record(tmp_path / _APPROVALS / "T-park-B.approved", _signed("T-park-A", "approved", ""))
        assert approval_decision_ref(tmp_path, "T-park-B") == ""
