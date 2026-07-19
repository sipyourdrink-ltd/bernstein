"""Durable task suspend and resume: attested park receipts (#2552).

These tests exercise the substrate-coupled guarantees the feature ships:

* **Fail closed** -- an infrastructure release with no suspend receipt is
  rejected before any effect runs.
* **Receipt before effect** -- the suspend row and its audit receipt land
  before the seat, sandbox, process, and envelope headroom are freed, and each
  release references the suspend receipt hash.
* **Determinism** -- two hosts derive the byte-identical resume decision hash
  from the same suspend row, and replay reproduces identical journal hashes up
  to the park boundary.
* **Verifiability** -- ``verify_suspension_continuity`` proves, offline from a
  copied chain, that a resume continued from exactly the parked workspace hash;
  mutating the suspend row fails journal verification at that exact index.
* **Correctness** -- released headroom equals reservation minus recorded spend,
  appears as a chained budget event, and the parked window carries zero spend.
* **Isolation** -- parking one worker leaves a second worker's journal, claim,
  and envelope untouched; the freed seat is dispatchable in the same tick.
* **Restart survival** -- a park persisted to the work ledger survives an
  orchestrator restart and resumes with the same continuity proof.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.cost.budget_actions import BudgetHeadroomReleaseEvent, compute_released_headroom
from bernstein.core.cost.cost_rollup_by_envelope import rollup
from bernstein.core.cost.cost_tracker import TokenUsage
from bernstein.core.persistence.work_ledger import (
    KIND_TASK_RESUMED,
    KIND_TASK_SUSPENDED,
    LedgerReader,
    WorkLedger,
    replay_state,
)
from bernstein.core.replay.journal import load_events, rebuild_state, verify_journal
from bernstein.core.security.audit_chain import (
    EVENT_TASK_RESOURCE_RELEASE,
    EVENT_TASK_RESUMED,
    EVENT_TASK_SUSPENDED,
    AuditChainStore,
)
from bernstein.core.tasks.checkpoint_retry import RetryMode, task_run_id
from bernstein.core.tasks.suspension import (
    CONTINUITY_FAILED,
    CONTINUITY_PENDING,
    CONTINUITY_VERIFIED,
    JOURNAL_EVENT_RESUME,
    RESOURCE_BUDGET,
    RESOURCE_PROCESS,
    RESOURCE_SANDBOX,
    RESOURCE_SEAT,
    WAKE_APPROVAL,
    ReleaseWithoutReceiptError,
    ResourceHandles,
    ResumeApprovalRequiredError,
    SuspendReceiptMismatchError,
    SuspendRow,
    SuspensionAlreadySettledError,
    UnsafeTaskIdError,
    approval_decision_ref,
    decide_resume,
    find_settlements,
    find_suspension_receipt,
    latest_suspension,
    park_task,
    release_resources,
    resume_task,
    validate_task_id,
    verify_suspension_continuity,
    write_resume_marker,
)

_KEY = b"0" * 32


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _worktree(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    wt = tmp_path / name
    wt.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return wt


class _SeatPool:
    """Minimal seat pool: a park returns a seat, a dispatch consumes one."""

    def __init__(self, total: int, used: int) -> None:
        self.total = total
        self.used = used

    @property
    def available(self) -> int:
        return self.total - self.used

    def return_seat(self) -> dict[str, object]:
        self.used -= 1
        return {"available_after": self.available}

    def dispatch(self) -> bool:
        if self.available <= 0:
            return False
        self.used += 1
        return True


# ---------------------------------------------------------------------------
# Fail closed: release with no receipt is rejected before any effect
# ---------------------------------------------------------------------------


def test_release_without_receipt_is_rejected(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    reaped: list[str] = []

    handles = ResourceHandles(
        reap_process=lambda: reaped.append("reaped") or {"method": "posix"},  # type: ignore[func-returns-value]
    )
    with pytest.raises(ReleaseWithoutReceiptError):
        release_resources(
            chain=chain,
            task_id="T-1",
            suspend_receipt_hash="",
            envelope="subscription",
            reserved_usd=10.0,
            spent_usd=2.0,
            handles=handles,
        )
    # No physical effect ran and no release row reached the chain.
    assert reaped == []
    assert chain.query(event_type=EVENT_TASK_RESOURCE_RELEASE) == []


# ---------------------------------------------------------------------------
# Receipt before effect + releases reference the receipt hash
# ---------------------------------------------------------------------------


def test_park_records_receipt_before_effect_and_releases_reference_it(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "print(1)\n"})

    handles = ResourceHandles(
        reap_process=lambda: {"method": "posix_process_group", "delivered": True},
        teardown_sandbox=lambda: {"backend": "worktree"},
        return_seat=lambda: {"available_after": 3},
    )
    result = park_task(
        sdd_dir=sdd,
        task_id="T-park",
        adapter="claude",
        session_id="sess-abc",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=10.0,
        spent_usd=2.5,
        chain=chain,
        handles=handles,
    )

    # The suspend receipt exists on the chain and the suspend row hashes to the
    # suspension's identity.
    suspend_rows = chain.query(event_type=EVENT_TASK_SUSPENDED)
    assert len(suspend_rows) == 1
    assert suspend_rows[0].details["suspend_event_hash"] == result.suspend_row.event_hash

    # The suspend row precedes every release row on the chain (receipt first).
    ordered = [e.event_type for e in chain.query()]
    first_release = ordered.index(EVENT_TASK_RESOURCE_RELEASE)
    assert ordered.index(EVENT_TASK_SUSPENDED) < first_release

    # Every release references the suspend receipt hash.
    releases = chain.query(event_type=EVENT_TASK_RESOURCE_RELEASE)
    resources = {r.details["resource"] for r in releases}
    assert resources == {RESOURCE_PROCESS, RESOURCE_SANDBOX, RESOURCE_SEAT, RESOURCE_BUDGET}
    for r in releases:
        assert r.details["suspend_receipt_hash"] == result.suspend_receipt_hash

    # The whole chain still verifies.
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# Correctness: released headroom = reserved - spent, chained budget event
# ---------------------------------------------------------------------------


def test_released_headroom_equals_reserved_minus_spent(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    result = park_task(
        sdd_dir=sdd,
        task_id="T-budget",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=12.0,
        spent_usd=4.25,
        chain=chain,
    )
    assert result.release.released_usd == pytest.approx(7.75)
    assert compute_released_headroom(12.0, 4.25) == pytest.approx(7.75)

    budget_rows = [
        r for r in chain.query(event_type=EVENT_TASK_RESOURCE_RELEASE) if r.details["resource"] == RESOURCE_BUDGET
    ]
    assert len(budget_rows) == 1
    detail = budget_rows[0].details["detail"]
    assert detail["released_usd"] == pytest.approx(7.75)
    assert detail["reserved_usd"] == pytest.approx(12.0)
    assert detail["spent_usd"] == pytest.approx(4.25)
    assert detail["suspend_receipt_hash"] == result.suspend_receipt_hash


def test_headroom_release_event_is_fail_closed() -> None:
    from bernstein.core.cost.budget_actions import build_headroom_release_event

    with pytest.raises(ValueError, match="fail closed"):
        build_headroom_release_event(
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=1.0,
            suspend_receipt_hash="",
        )
    ok = build_headroom_release_event(
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=1.0,
        suspend_receipt_hash="abc",
    )
    assert isinstance(ok, BudgetHeadroomReleaseEvent)
    assert ok.released_usd == pytest.approx(4.0)


def test_parked_window_attributes_zero_spend(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    # Spend recorded before the park and after the resume; the parked window
    # carries no cost records at all.
    records = [
        TokenUsage(100, 50, "claude", 1.0, "agent", "T-window", timestamp=100.0, quota_envelope="subscription"),
        TokenUsage(100, 50, "claude", 3.0, "agent", "T-window", timestamp=400.0, quota_envelope="subscription"),
    ]
    park_ts, resume_ts = 200.0, 300.0
    park_task(
        sdd_dir=sdd,
        task_id="T-window",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=10.0,
        spent_usd=1.0,
        chain=chain,
    )
    parked_window = [r for r in records if park_ts <= r.timestamp < resume_ts]
    assert parked_window == []
    rolled = rollup(records)
    # Total spend is the pre + post records; nothing is attributed inside the
    # parked window because nothing was recorded there.
    assert rolled["subscription"].total_spend == pytest.approx(4.0)
    assert sum(r.cost_usd for r in parked_window) == 0.0


# ---------------------------------------------------------------------------
# Determinism: two hosts derive the byte-identical resume decision
# ---------------------------------------------------------------------------


def test_resume_decision_is_deterministic_across_hosts(tmp_path: Path) -> None:
    from bernstein.core.tasks.checkpoint_retry import workspace_hash

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    files = {"a.py": "print('hi')\n", "pkg/b.py": "y = 2\n"}
    wt = _worktree(tmp_path, "wt", files)

    # Park once. The suspend row travels with the run (journal + work ledger),
    # so both hosts hold the byte-identical row.
    park = park_task(
        sdd_dir=sdd,
        task_id="T-det",
        adapter="claude",
        session_id="sess-1",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=8.0,
        spent_usd=1.0,
        chain=chain,
    )

    # Two hosts re-materialize the worktree at *different physical paths* with
    # byte-identical content. The workspace hash is content-addressed, so both
    # derive the same decision hash from the same suspend row.
    host_a = _worktree(tmp_path, "host-a-wt", files)
    host_b = _worktree(tmp_path, "host-b-wt", files)
    dec_a = decide_resume(suspend_row=park.suspend_row, actual_workspace_hash=workspace_hash(host_a))
    dec_b = decide_resume(suspend_row=park.suspend_row, actual_workspace_hash=workspace_hash(host_b))
    assert dec_a.decision_hash == dec_b.decision_hash
    assert dec_a.effective_mode is RetryMode.WARM
    assert dec_b.effective_mode is RetryMode.WARM


def test_replay_reproduces_identical_journal_hashes_to_park_boundary(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-replay",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )
    journal_path = sdd / "runs" / task_run_id("T-replay") / "journal.jsonl"
    boundary = park.suspend_row.journal_index + 1

    first = rebuild_state(journal_path, from_step=boundary)
    second = rebuild_state(journal_path, from_step=boundary)
    assert first["head_hash"] == second["head_hash"]
    # Head over the prefix ending at the suspend row equals the row's identity.
    assert first["head_hash"] == park.suspend_row.event_hash


# ---------------------------------------------------------------------------
# Verifiability: continuity proof + tamper detection
# ---------------------------------------------------------------------------


def test_verify_continuity_proves_warm_resume(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-cont",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )
    # Resume from the unchanged worktree -> warm continuation.
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    assert resume.decision.effective_mode is RetryMode.WARM

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-cont", chain=chain)
    assert result.ok, result.errors
    assert result.resumed
    assert result.effective_mode == "warm"
    assert result.workspace_match


def test_verify_continuity_shows_cold_downgrade_with_reason(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-drift",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )
    # Drift the worktree before resume -> the workspace hash no longer matches.
    (wt / "a.py").write_text("x = 999\n", encoding="utf-8")
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    assert resume.decision.effective_mode is RetryMode.COLD
    assert resume.decision.downgrade_reason == "workspace_hash_mismatch"

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-drift", chain=chain)
    assert result.ok, result.errors
    assert result.effective_mode == "cold"
    assert not result.workspace_match
    assert result.downgrade_reason == "workspace_hash_mismatch"


def test_verify_continuity_accepts_honored_fork(tmp_path: Path) -> None:
    # A fork requested against a fork-capable adapter with a matching workspace
    # is an honored continuation, not a downgrade: it carries no reason and the
    # verifier must not treat the empty reason as a failure.
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-fork",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        requested_mode=RetryMode.FORK,
    )
    assert resume.decision.effective_mode is RetryMode.FORK
    assert resume.decision.downgrade_reason == ""

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-fork", chain=chain)
    assert result.ok, result.errors
    assert result.effective_mode == "fork"
    assert result.workspace_match
    assert result.downgrade_reason == ""


def test_mutating_suspend_row_fails_verification_at_that_index(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-tamper",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )
    journal_path = sdd / "runs" / task_run_id("T-tamper") / "journal.jsonl"

    # Mutate the parked workspace hash on the suspend row on disk.
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    idx = park.suspend_row.journal_index
    row = json.loads(lines[idx])
    row["workspace_hash"] = "deadbeef" * 8
    lines[idx] = json.dumps(row)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verify = verify_journal(journal_path)
    assert not verify.ok
    assert verify.divergent_index == idx

    # A tampered suspend row can never fuel a resume: fail-closed read returns None.
    assert latest_suspension(sdd, "T-tamper") is None
    # And the continuity verifier reports the journal break.
    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-tamper", chain=chain)
    assert not result.ok
    assert not result.journal_ok


def test_mutating_suspend_receipt_breaks_hmac_chain(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park_task(
        sdd_dir=sdd,
        task_id="T-hmac",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=1.0,
        chain=chain,
    )
    # Tamper the suspend receipt payload directly in the audit log file.
    audit_files = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert audit_files
    log = audit_files[0]
    lines = log.read_text(encoding="utf-8").splitlines()
    for i, raw in enumerate(lines):
        entry = json.loads(raw)
        if entry.get("event_type") == EVENT_TASK_SUSPENDED:
            entry["details"]["released_usd"] = 999.0
            lines[i] = json.dumps(entry)
            break
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = AuditChainStore(tmp_path / "audit", key=_KEY)
    ok, errors = reopened.verify()
    assert not ok
    assert errors


# ---------------------------------------------------------------------------
# Isolation: parking one worker leaves a second untouched; freed seat dispatches
# ---------------------------------------------------------------------------


def test_isolation_two_workers_and_seat_redispatch(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt_a = _worktree(tmp_path, "wt-a", {"a.py": "a = 1\n"})
    wt_b = _worktree(tmp_path, "wt-b", {"b.py": "b = 2\n"})
    pool = _SeatPool(total=2, used=2)  # both seats occupied
    assert pool.available == 0

    park_task(
        sdd_dir=sdd,
        task_id="T-a",
        adapter="claude",
        session_id="sa",
        worktree_path=wt_a,
        envelope="env-a",
        reserved_usd=6.0,
        spent_usd=1.0,
        chain=chain,
        handles=ResourceHandles(return_seat=pool.return_seat),
    )

    # Worker B's worktree, journal, and envelope are untouched.
    assert wt_b.read_text() if False else (wt_b / "b.py").read_text() == "b = 2\n"
    assert latest_suspension(sdd, "T-b") is None
    b_journal = sdd / "runs" / task_run_id("T-b") / "journal.jsonl"
    assert not b_journal.exists()

    # The freed seat is dispatchable to a queued task in the same tick.
    assert pool.available == 1
    assert pool.dispatch() is True
    assert pool.available == 0


# ---------------------------------------------------------------------------
# --until approval: resume when approve lands; receipts reference each other
# ---------------------------------------------------------------------------


def test_until_approval_resumes_when_approval_lands(tmp_path: Path) -> None:
    workdir = tmp_path
    sdd = workdir / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-appr",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
        wake_condition=WAKE_APPROVAL,
    )
    assert park.suspend_row.wake_condition == WAKE_APPROVAL

    # Before approval there is no decision to bind.
    assert approval_decision_ref(workdir, "T-appr") == ""

    # bernstein approve lands the decision file.
    approvals = sdd / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-appr.approved").write_text("approved", encoding="utf-8")
    approval_ref = approval_decision_ref(workdir, "T-appr")
    assert approval_ref  # non-empty digest binding the approval record

    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        approval_ref=approval_ref,
    )
    marker = write_resume_marker(workdir, "T-appr", resume.resume_receipt_hash)

    # The resume receipt references the approval decision...
    resume_rows = chain.query(event_type=EVENT_TASK_RESUMED)
    assert resume_rows[-1].details["approval_ref"] == approval_ref
    # ...and the approval side references the resume receipt (back-reference).
    assert marker.read_text(encoding="utf-8") == resume.resume_receipt_hash


# ---------------------------------------------------------------------------
# Restart survival: park persists to the ledger, resume after a fresh open
# ---------------------------------------------------------------------------


def test_suspended_state_survives_orchestrator_restart(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    ledger_dir = sdd / "runtime" / "ledger" / "run-1"

    ledger = WorkLedger.open(ledger_dir)
    park = park_task(
        sdd_dir=sdd,
        task_id="T-restart",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=1.0,
        chain=chain,
        ledger=ledger,
    )
    assert park.ledger_entry_hash

    # Simulate a restart: drop every in-memory handle and re-open from disk.
    del ledger
    reopened = WorkLedger.open(ledger_dir)
    state = replay_state(LedgerReader(ledger_dir).entries(), run_id="run-1")
    assert "T-restart" in state.suspended_tasks
    assert "T-restart" not in state.resume_frontier()  # parked tasks wake explicitly

    # The suspend row is still readable from disk with the same identity.
    reloaded = latest_suspension(sdd, "T-restart")
    assert reloaded is not None
    assert reloaded.event_hash == park.suspend_row.event_hash

    # Resume succeeds with the same continuity proof (identical decision hash).
    dec_before = decide_resume(suspend_row=park.suspend_row, actual_workspace_hash=park.suspend_row.workspace_hash)
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=reloaded,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        ledger=reopened,
    )
    assert resume.decision.decision_hash == dec_before.decision_hash

    final = replay_state(LedgerReader(ledger_dir).entries(), run_id="run-1")
    assert "T-restart" not in final.suspended_tasks
    assert "T-restart" in final.in_flight_tasks  # resumed -> started

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-restart", chain=chain)
    assert result.ok, result.errors


def test_work_ledger_kinds_project_to_states(tmp_path: Path) -> None:
    ledger = WorkLedger.open(tmp_path / "ledger")
    ledger.append(kind=KIND_TASK_SUSPENDED, task_id="T-x", payload={"a": 1})
    state = replay_state(LedgerReader(tmp_path / "ledger").entries())
    assert state.suspended_tasks == ["T-x"]
    ledger.append(kind=KIND_TASK_RESUMED, task_id="T-x", payload={"b": 2})
    state2 = replay_state(LedgerReader(tmp_path / "ledger").entries())
    assert state2.suspended_tasks == []
    assert state2.in_flight_tasks == ["T-x"]


# ---------------------------------------------------------------------------
# Hardening (#2636): receipt binding, approval gating, path containment,
# and continuity proofs that refuse incomplete or unrelated evidence.
# ---------------------------------------------------------------------------


def _park(tmp_path: Path, chain: AuditChainStore, task_id: str, worktree: Path) -> object:
    return park_task(
        sdd_dir=tmp_path / ".sdd",
        task_id=task_id,
        adapter="claude",
        session_id="s",
        worktree_path=worktree,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )


def _journal_rows(sdd: Path, task_id: str) -> list[dict[str, object]]:
    path = sdd / "runs" / task_run_id(task_id) / "journal.jsonl"
    return list(load_events(path)) if path.exists() else []


def test_resume_rejects_receipt_from_a_different_suspend_row(tmp_path: Path) -> None:
    """A receipt bound to another suspend row must not drive a resume.

    The suspension's identity is the suspend row's ``event_hash``; a receipt
    that binds a *different* row is not evidence for this park. The refusal
    lands before any journal mutation.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    first = _park(tmp_path, chain, "T-sub", wt)
    second = _park(tmp_path, chain, "T-sub", wt)
    assert first.suspend_row.event_hash != second.suspend_row.event_hash

    rows_before = len(_journal_rows(sdd, "T-sub"))
    with pytest.raises(SuspendReceiptMismatchError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=first.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=second.suspend_receipt_hash,
        )
    # Fail closed: no resume row, no resume receipt.
    assert len(_journal_rows(sdd, "T-sub")) == rows_before
    assert chain.query(event_type=EVENT_TASK_RESUMED) == []


def test_resume_rejects_receipt_from_a_different_task(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    victim = _park(tmp_path, chain, "T-victim", wt)
    foreign = _park(tmp_path, chain, "T-foreign", wt)

    with pytest.raises(SuspendReceiptMismatchError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=victim.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=foreign.suspend_receipt_hash,
        )
    assert chain.query(event_type=EVENT_TASK_RESUMED) == []


def test_resume_rejects_a_receipt_hash_absent_from_the_chain(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-ghost", wt)

    with pytest.raises(SuspendReceiptMismatchError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=park.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash="f" * 64,
        )
    assert chain.query(event_type=EVENT_TASK_RESUMED) == []


def test_resume_refuses_forged_suspend_receipt_with_invalid_hmac(tmp_path: Path) -> None:
    """A forged suspend receipt (invalid HMAC) must never drive a real resume.

    Threat model: an actor with write access to the project ``.sdd/`` and
    ``audit/`` store -- e.g. a semi-trusted worker sabotaging another task.
    Such an actor holds no audit key, so they can:

    * append a ``task.suspend`` row to the victim's run journal (the journal is
      a keyless SHA-256 chain -- tamper-evident, not forgery-resistant), and
    * append a ``task.suspend_receipt`` row to the audit store bearing an
      attacker-chosen ``hmac`` (invalid, because they cannot sign it).

    The audit-chain HMAC is the only forgery-resistant check in the flow. The
    resume path must authenticate the receipt against it before honoring it, so
    a receipt whose stored HMAC does not recompute is refused rather than
    matched by string equality.
    """
    from bernstein.core.tasks.checkpoint_retry import workspace_hash
    from bernstein.core.tasks.suspension import record_task_suspension_row

    sdd = tmp_path / ".sdd"
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    # (1) Fabricate a journal suspend row for the victim task. This writes the
    #     keyless SHA-256 chain an attacker with journal write-access can compute
    #     and, crucially, no audit receipt -- so T-x is not legitimately resumable.
    record_task_suspension_row(
        sdd_dir=sdd,
        task_id="T-x",
        adapter="claude",
        session_id="s",
        workspace_hash=workspace_hash(wt),
        worktree_path=str(wt),
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        released_usd=5.0,
    )
    # The journal verifies (keyless), so the fail-closed reader hands the row back.
    row = latest_suspension(sdd, "T-x")
    assert row is not None

    # (2) Forge a task.suspend_receipt on the audit store binding the victim task
    #     and the forged journal row, with an INVALID hmac (no key to sign it).
    forged_hmac = "f" * 64
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": "2026-07-19T00:00:00.000000Z",
        "event_type": EVENT_TASK_SUSPENDED,
        "actor": "attacker",
        "resource_type": "task",
        "resource_id": "T-x",
        "details": {
            "task_id": "T-x",
            "suspend_event_hash": row.event_hash,
            "journal_index": row.journal_index,
        },
        "prev_hmac": "0" * 64,
        "hmac": forged_hmac,
    }
    (audit_dir / "2026-07-19.jsonl").write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")

    chain = AuditChainStore(audit_dir, key=_KEY)
    # The unauthenticated read the vulnerable path used *does* surface the forged
    # row -- this is exactly what let the old string-equality match succeed.
    assert any(e.hmac == forged_hmac for e in chain.query(event_type=EVENT_TASK_SUSPENDED))
    # And the chain does not verify: the forged HMAC is the break.
    ok, _errs = chain.verify()
    assert not ok

    # (3) The real resume path must refuse the forged receipt, before any mutation.
    rows_before = len(_journal_rows(sdd, "T-x"))
    with pytest.raises(SuspendReceiptMismatchError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=forged_hmac,
        )
    # Fail closed: no resume row, no resume receipt reached either store.
    assert len(_journal_rows(sdd, "T-x")) == rows_before
    assert chain.query(event_type=EVENT_TASK_RESUMED) == []


def test_resume_succeeds_with_valid_receipt_on_untampered_chain(tmp_path: Path) -> None:
    """The authenticated read must not break the happy path.

    A receipt with a valid HMAC on an untampered chain resumes exactly as
    before: this is the direct counterpart to the forged-receipt refusal.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-happy", wt)

    result = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    assert result.resume_receipt_hash
    resumed = chain.query(event_type=EVENT_TASK_RESUMED)
    assert len(resumed) == 1
    assert resumed[0].details.get("suspend_receipt_hash") == park.suspend_receipt_hash


def test_release_rejects_a_receipt_bound_to_another_task(tmp_path: Path) -> None:
    """A release must reference *this* task's receipt, not merely a non-empty one."""
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    foreign = _park(tmp_path, chain, "T-owner", wt)
    releases_before = len(chain.query(event_type=EVENT_TASK_RESOURCE_RELEASE))

    reaped: list[str] = []
    handles = ResourceHandles(reap_process=lambda: (reaped.append("pid"), {"pid": 1})[1])
    with pytest.raises(SuspendReceiptMismatchError):
        release_resources(
            chain=chain,
            task_id="T-other",
            suspend_receipt_hash=foreign.suspend_receipt_hash,
            envelope="subscription",
            reserved_usd=10.0,
            spent_usd=2.0,
            handles=handles,
        )
    # No physical effect ran and no release row reached the chain.
    assert reaped == []
    assert len(chain.query(event_type=EVENT_TASK_RESOURCE_RELEASE)) == releases_before


def test_find_suspension_receipt_selects_the_receipt_for_the_given_row(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    first = _park(tmp_path, chain, "T-pick", wt)
    second = _park(tmp_path, chain, "T-pick", wt)

    assert find_suspension_receipt(chain=chain, task_id="T-pick", suspend_row=first.suspend_row) is not None
    picked_first = find_suspension_receipt(chain=chain, task_id="T-pick", suspend_row=first.suspend_row)
    picked_second = find_suspension_receipt(chain=chain, task_id="T-pick", suspend_row=second.suspend_row)
    assert picked_first is not None and picked_second is not None
    assert picked_first.hmac == first.suspend_receipt_hash
    assert picked_second.hmac == second.suspend_receipt_hash


def test_resume_without_approval_is_rejected_before_any_journal_mutation(tmp_path: Path) -> None:
    """An ``--until approval`` park refuses to resume until the decision lands."""
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-gate",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
        wake_condition=WAKE_APPROVAL,
    )
    rows_before = len(_journal_rows(sdd, "T-gate"))

    with pytest.raises(ResumeApprovalRequiredError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=park.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=park.suspend_receipt_hash,
            approval_ref="",
        )
    assert len(_journal_rows(sdd, "T-gate")) == rows_before
    assert chain.query(event_type=EVENT_TASK_RESUMED) == []

    # Once the decision lands the same resume succeeds.
    approvals = sdd / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-gate.approved").write_text("approved", encoding="utf-8")
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        approval_ref=approval_decision_ref(tmp_path, "T-gate"),
    )
    assert resume.resume_receipt_hash


@pytest.mark.parametrize(
    "bad_task_id",
    [
        "../../../etc/passwd",
        "..",
        ".",
        "../escape",
        "sub/dir",
        "/absolute",
        "with space",
        "",
        "back\\slash",
    ],
)
def test_approval_paths_refuse_unsafe_task_ids(tmp_path: Path, bad_task_id: str) -> None:
    """``task_id`` is an identifier, never a path fragment."""
    with pytest.raises(UnsafeTaskIdError):
        approval_decision_ref(tmp_path, bad_task_id)
    with pytest.raises(UnsafeTaskIdError):
        write_resume_marker(tmp_path, bad_task_id, "deadbeef")


def test_resume_marker_never_escapes_the_approvals_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(UnsafeTaskIdError):
        write_resume_marker(tmp_path, "../../outside/pwned", "deadbeef")
    assert list(outside.iterdir()) == []


def test_approval_paths_accept_ordinary_task_ids(tmp_path: Path) -> None:
    approvals = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-abc_123.def.approved").write_text("approved", encoding="utf-8")
    assert approval_decision_ref(tmp_path, "T-abc_123.def")
    marker = write_resume_marker(tmp_path, "T-abc_123.def", "cafe")
    assert marker.read_text(encoding="utf-8") == "cafe"


def test_resume_refuses_an_unsafe_task_id_on_the_suspend_row(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-ok", wt)
    forged = SuspendRow(**{**park.suspend_row.to_dict(), "task_id": "../../escape"})

    with pytest.raises(UnsafeTaskIdError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=forged,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=park.suspend_receipt_hash,
        )


def test_verify_continuity_reports_an_unresumed_park_as_pending_not_failed(tmp_path: Path) -> None:
    """A live park is an incomplete lifecycle, not a broken proof.

    It must not claim a verified continuity (the original defect), and it must
    not report as a failure either: an operator sweeping a fleet with live
    parks would drown the real breaks in false alarms.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    _park(tmp_path, chain, "T-open", wt)

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-open", chain=chain)
    assert result.status == CONTINUITY_PENDING
    assert result.pending
    assert not result.verified
    assert not result.resumed
    # Not a failure: no errors, and ok stays true so a sweep is not flooded.
    assert result.ok
    assert result.errors == []
    # The distinction is machine-readable, not prose.
    assert result.to_dict()["status"] == CONTINUITY_PENDING


def test_pending_and_failed_are_distinguishable_without_reading_messages(tmp_path: Path) -> None:
    """The three outcomes are separable programmatically."""
    from bernstein.core.security.audit_chain import record_task_resume

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    # Pending: parked, never settled.
    _park(tmp_path, chain, "T-live", wt)
    pending = verify_suspension_continuity(sdd_dir=sdd, task_id="T-live", chain=chain)

    # Verified: parked and settled cleanly.
    settled = _park(tmp_path, chain, "T-done", wt)
    resume_task(
        sdd_dir=sdd,
        suspend_row=settled.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=settled.suspend_receipt_hash,
    )
    verified = verify_suspension_continuity(sdd_dir=sdd, task_id="T-done", chain=chain)

    # Failed: a settlement claimed against a foreign receipt.
    broken = _park(tmp_path, chain, "T-bad", wt)
    decoy = _park(tmp_path, chain, "T-decoy3", wt)
    record_task_resume(
        chain=chain,
        task_id="T-bad",
        suspend_receipt_hash=decoy.suspend_receipt_hash,
        suspend_event_hash=broken.suspend_row.event_hash,
        resume_event_hash="0" * 64,
        journal_index=99,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=broken.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash="0" * 64,
    )
    failed = verify_suspension_continuity(sdd_dir=sdd, task_id="T-bad", chain=chain)

    assert (pending.status, verified.status, failed.status) == (
        CONTINUITY_PENDING,
        CONTINUITY_VERIFIED,
        CONTINUITY_FAILED,
    )
    # ok separates "nothing broken" from "broken"; status separates the rest.
    assert pending.ok and verified.ok and not failed.ok
    assert not pending.resumed and verified.resumed and not failed.resumed


def test_second_park_is_pending_while_the_first_park_is_settled(tmp_path: Path) -> None:
    """Resume receipts that claim a *different* row leave this park pending.

    Without the claim check, the earlier park's resume receipt would look like
    a foreign settlement of the live park and report a false failure.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    first = _park(tmp_path, chain, "T-again", wt)
    resume_task(
        sdd_dir=sdd,
        suspend_row=first.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=first.suspend_receipt_hash,
    )
    assert verify_suspension_continuity(sdd_dir=sdd, task_id="T-again", chain=chain).verified

    # Park it again; the new park has not settled.
    _park(tmp_path, chain, "T-again", wt)
    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-again", chain=chain)
    assert result.status == CONTINUITY_PENDING, result.errors
    assert result.ok
    assert result.errors == []


def test_verify_suspension_cli_exit_codes_are_stable_across_states(tmp_path: Path) -> None:
    """Live parks keep exit 0; only a real break exits 1."""
    from click.testing import CliRunner

    from bernstein.cli.commands.audit_cmd import audit_group
    from bernstein.cli.commands.task_cmd import task_group

    root = tmp_path
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    runner = CliRunner()

    parked = runner.invoke(task_group, ["suspend", "T-exit", "--workdir", str(root), "--worktree", str(wt), "--json"])
    assert parked.exit_code == 0, parked.output

    # Parked, not resumed: exit 0, and the text says so rather than claiming
    # a verified continuity.
    pending = runner.invoke(audit_group, ["verify-suspension", "T-exit", "--workdir", str(root)])
    assert pending.exit_code == 0, pending.output
    assert "not settled yet" in " ".join(pending.output.split())

    pending_json = runner.invoke(audit_group, ["verify-suspension", "T-exit", "--workdir", str(root), "--json"])
    assert pending_json.exit_code == 0
    assert '"status": "pending"' in " ".join(pending_json.output.split())

    # Resumed cleanly: still exit 0, now reported as verified.
    resumed = runner.invoke(task_group, ["resume", "T-exit", "--workdir", str(root), "--worktree", str(wt), "--json"])
    assert resumed.exit_code == 0, resumed.output
    done = runner.invoke(audit_group, ["verify-suspension", "T-exit", "--workdir", str(root)])
    assert done.exit_code == 0, done.output
    assert "continuity verified" in " ".join(done.output.split()).lower()

    # A task with no suspend receipt at all is still a failure: exit 1.
    missing = runner.invoke(audit_group, ["verify-suspension", "T-nosuch", "--workdir", str(root)])
    assert missing.exit_code == 1


def test_verify_continuity_rejects_a_resume_bound_to_a_foreign_receipt(tmp_path: Path) -> None:
    """A resume receipt must hang off *this* park's suspend receipt."""
    from bernstein.core.security.audit_chain import record_task_resume

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-forge", wt)
    decoy = _park(tmp_path, chain, "T-decoy", wt)

    # Names the parked row but hangs off a receipt from a different park.
    record_task_resume(
        chain=chain,
        task_id="T-forge",
        suspend_receipt_hash=decoy.suspend_receipt_hash,
        suspend_event_hash=park.suspend_row.event_hash,
        resume_event_hash="0" * 64,
        journal_index=99,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=park.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash="0" * 64,
    )

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-forge", chain=chain)
    assert not result.ok
    # Assert *which* guard fired. Without this the test passes on the phantom
    # journal-row check alone and the binding filter ships uncovered.
    assert any("settlement record is inconsistent" in err for err in result.errors), result.errors
    assert not result.resumed


def test_verify_continuity_rejects_a_resume_row_absent_from_the_journal(tmp_path: Path) -> None:
    """The receipt's resume row must exist in the task journal it claims."""
    from bernstein.core.security.audit_chain import record_task_resume

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-phantom", wt)

    record_task_resume(
        chain=chain,
        task_id="T-phantom",
        suspend_receipt_hash=park.suspend_receipt_hash,
        suspend_event_hash=park.suspend_row.event_hash,
        resume_event_hash="1" * 64,
        journal_index=99,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=park.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash="1" * 64,
    )

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-phantom", chain=chain)
    assert not result.ok
    assert any("journal" in err for err in result.errors)


def test_receipt_selection_ignores_a_later_receipt_for_another_row(tmp_path: Path) -> None:
    """Selection is by binding, not by recency.

    This is the substitution the CLI resume path used to be open to: it took
    the *latest* ``task.suspend_receipt`` for the task, so a later receipt
    naming some other suspend row was accepted as evidence for the parked one.
    """
    from bernstein.core.security.audit_chain import record_task_suspension

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-inject", wt)

    # A later receipt for the same task that binds a row the journal never had.
    injected = record_task_suspension(
        chain=chain,
        task_id="T-inject",
        suspend_event_hash="9" * 64,
        journal_index=99,
        adapter="claude",
        workspace_hash=park.suspend_row.workspace_hash,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        released_usd=5.0,
    )
    assert injected.hmac != park.suspend_receipt_hash

    picked = find_suspension_receipt(chain=chain, task_id="T-inject", suspend_row=park.suspend_row)
    assert picked is not None
    assert picked.hmac == park.suspend_receipt_hash

    # And the injected receipt cannot itself drive the resume.
    with pytest.raises(SuspendReceiptMismatchError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=park.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=injected.hmac,
        )


# ---------------------------------------------------------------------------
# Follow-up hardening (#2636 review): single-use settlement, and coverage for
# the guards the first round left unpinned.
# ---------------------------------------------------------------------------


def test_one_approval_authorises_exactly_one_resume(tmp_path: Path) -> None:
    """An approval settles a park once; it is not a reusable permission.

    Regression for the replay hole: the wake gate used to be a presence check
    on the decision file, and nothing consumed it, so a single operator
    approval authorised an unbounded number of resume rows.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-once",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
        wake_condition=WAKE_APPROVAL,
    )
    approvals = sdd / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-once.approved").write_text("approved", encoding="utf-8")
    approval_ref = approval_decision_ref(tmp_path, "T-once")

    first = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        approval_ref=approval_ref,
    )
    assert first.resume_receipt_hash

    rows_after_first = len(_journal_rows(sdd, "T-once"))
    # The same approval file is still on disk and still yields the same digest,
    # but the park is spent.
    assert approval_decision_ref(tmp_path, "T-once") == approval_ref
    with pytest.raises(SuspensionAlreadySettledError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=park.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=park.suspend_receipt_hash,
            approval_ref=approval_ref,
        )
    assert len(_journal_rows(sdd, "T-once")) == rows_after_first
    assert len(chain.query(event_type=EVENT_TASK_RESUMED)) == 1


def test_settled_park_refuses_replay_through_the_cli(tmp_path: Path) -> None:
    """End-to-end: three CLI resumes against one approval yield one settlement."""
    from click.testing import CliRunner

    from bernstein.cli.commands.task_cmd import task_group

    root = tmp_path
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    runner = CliRunner()

    parked = runner.invoke(
        task_group,
        ["suspend", "T-cli", "--workdir", str(root), "--worktree", str(wt), "--until", "approval", "--json"],
    )
    assert parked.exit_code == 0, parked.output

    approvals = root / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-cli.approved").write_text("approved", encoding="utf-8")

    outcomes = [
        runner.invoke(task_group, ["resume", "T-cli", "--workdir", str(root), "--worktree", str(wt), "--json"])
        for _ in range(3)
    ]
    assert outcomes[0].exit_code == 0, outcomes[0].output
    assert outcomes[1].exit_code == 1
    assert outcomes[2].exit_code == 1
    # The console wraps, so compare against whitespace-normalised output.
    assert "already settled" in " ".join(outcomes[1].output.split())

    chain = AuditChainStore(root / ".sdd" / "audit")
    assert len(chain.query(event_type=EVENT_TASK_RESUMED)) == 1

    result = verify_suspension_continuity(sdd_dir=root / ".sdd", task_id="T-cli", chain=chain)
    assert result.ok, result.errors


def test_verify_continuity_flags_a_park_settled_more_than_once(tmp_path: Path) -> None:
    """A chain carrying two settlements of one park is not a clean proof.

    The resume path refuses the second settlement, so this shape only reaches
    the verifier on a chain written before the guard existed or assembled by
    hand. The offline proof must still refuse it rather than reporting the last
    settlement as a clean continuity.
    """
    from bernstein.core.security.audit_chain import record_task_resume

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-twice", wt)
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    assert verify_suspension_continuity(sdd_dir=sdd, task_id="T-twice", chain=chain).ok

    # A second settlement of the same park, otherwise well-formed.
    record_task_resume(
        chain=chain,
        task_id="T-twice",
        suspend_receipt_hash=park.suspend_receipt_hash,
        suspend_event_hash=park.suspend_row.event_hash,
        resume_event_hash=resume.resume_event_hash,
        journal_index=1,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=park.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash=resume.decision.decision_hash,
    )

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-twice", chain=chain)
    assert not result.ok
    assert any("settled 2 times" in err for err in result.errors), result.errors


def test_reported_state_comes_from_the_genuine_settlement_not_the_forgery(tmp_path: Path) -> None:
    """Binding selection, pinned independently of the journal-row check.

    The forged receipt names a resume row the journal really holds, so the
    ``_row_present`` guard cannot reject it. Two things must hold at once: the
    forgery is *surfaced* as inconsistent evidence (it names this park's row
    but hangs off another park's receipt), and the *reported* continuation
    state is still read from the genuine settlement rather than from whichever
    receipt happens to be last on the chain.
    """
    from bernstein.core.security.audit_chain import record_task_resume

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-bind", wt)
    decoy = _park(tmp_path, chain, "T-decoy2", wt)
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )

    # Real resume row, real parked row, but hung off the decoy park's receipt.
    record_task_resume(
        chain=chain,
        task_id="T-bind",
        suspend_receipt_hash=decoy.suspend_receipt_hash,
        suspend_event_hash=park.suspend_row.event_hash,
        resume_event_hash=resume.resume_event_hash,
        journal_index=1,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=False,
        new_workspace_hash="deadbeef",
        downgrade_reason="",
        decision_hash="deadbeef",
    )

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-bind", chain=chain)
    # The forgery is surfaced rather than silently ignored...
    assert not result.ok
    assert any("settlement record is inconsistent" in err for err in result.errors), result.errors
    # ...and the reported state still comes from the genuine settlement. If
    # selection fell back to recency, these would carry the forgery's values
    # (workspace_match=False, new_workspace_hash="deadbeef").
    assert result.resumed
    assert result.workspace_match
    assert result.effective_mode == "warm"


def test_park_refuses_an_unsafe_task_id_before_writing_anything(tmp_path: Path) -> None:
    """The park-boundary guard, previously asserted only in the docstring."""
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    with pytest.raises(UnsafeTaskIdError):
        park_task(
            sdd_dir=sdd,
            task_id="../../escape",
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=1.0,
            spent_usd=0.0,
            chain=chain,
        )
    assert not (sdd / "runs").exists()
    assert chain.query(event_type=EVENT_TASK_SUSPENDED) == []


def test_task_id_bound_matches_the_journal_run_id_budget(tmp_path: Path) -> None:
    """An over-long id is refused here, not by a bare ValueError downstream.

    ``task_run_id`` prefixes ``"task-"`` and the journal caps the run id at 64
    characters, so the validator must refuse at 59.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    assert validate_task_id("T" + "a" * 58)  # 59 chars: the budget, accepted
    with pytest.raises(UnsafeTaskIdError):
        validate_task_id("T" + "a" * 59)  # 60 chars: one over

    # The park refuses with the typed error rather than the journal's ValueError.
    with pytest.raises(UnsafeTaskIdError):
        park_task(
            sdd_dir=sdd,
            task_id="T" + "a" * 90,
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=1.0,
            spent_usd=0.0,
            chain=chain,
        )


def test_longest_accepted_task_id_survives_a_full_park_and_resume(tmp_path: Path) -> None:
    """The accepted bound is genuinely usable end to end, not just accepted."""
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    task_id = "T" + "a" * 58

    park = park_task(
        sdd_dir=sdd,
        task_id=task_id,
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
    )
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    assert resume.resume_receipt_hash
    assert verify_suspension_continuity(sdd_dir=sdd, task_id=task_id, chain=chain).ok


def test_approval_paths_refuse_a_symlinked_decision_file(tmp_path: Path) -> None:
    """Containment catches what the identifier allowlist cannot.

    ``T-sym`` is a perfectly well-formed task id, so the allowlist admits it.
    The escape is in the filesystem: the decision file is a symlink out of the
    approvals directory. Without the resolved-path containment check, reading
    the approval would hash an arbitrary file's contents and writing the resume
    marker would clobber an arbitrary path.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("not an approval", encoding="utf-8")
    target = outside / "clobber.txt"
    target.write_text("original", encoding="utf-8")

    approvals = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-sym.approved").symlink_to(secret)
    (approvals / "T-sym.resumed").symlink_to(target)

    with pytest.raises(UnsafeTaskIdError):
        approval_decision_ref(tmp_path, "T-sym")
    with pytest.raises(UnsafeTaskIdError):
        write_resume_marker(tmp_path, "T-sym", "deadbeef")

    # The outside file was neither read into a digest nor overwritten.
    assert target.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# Follow-up review: the writer and the verifier must share one definition of
# "settled", one scope (every park), and one id-validation rule.
# ---------------------------------------------------------------------------


def _forge_settlement(chain: AuditChainStore, task_id: str, park: Any, resume_event_hash: str, count: int = 1) -> None:
    from bernstein.core.security.audit_chain import record_task_resume

    for _ in range(count):
        record_task_resume(
            chain=chain,
            task_id=task_id,
            suspend_receipt_hash=park.suspend_receipt_hash,
            suspend_event_hash=park.suspend_row.event_hash,
            resume_event_hash=resume_event_hash,
            journal_index=1,
            effective_mode="warm",
            requested_mode="warm",
            workspace_match=True,
            new_workspace_hash=park.suspend_row.workspace_hash,
            downgrade_reason="",
            decision_hash="x" * 64,
        )


def test_parking_again_does_not_erase_multi_settlement_detection(tmp_path: Path) -> None:
    """The proof covers every park, not just the latest one.

    Scoping to the newest suspend receipt meant an ordinary 'park again' -- the
    remediation documented for a spent park -- stopped the verifier looking at
    earlier parks, so replay damage already on the chain laundered itself.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})

    first = _park(tmp_path, chain, "T-relaunder", wt)
    resume = resume_task(
        sdd_dir=sdd,
        suspend_row=first.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=first.suspend_receipt_hash,
    )
    _forge_settlement(chain, "T-relaunder", first, resume.resume_event_hash, count=2)

    before = verify_suspension_continuity(sdd_dir=sdd, task_id="T-relaunder", chain=chain)
    assert before.status == CONTINUITY_FAILED
    assert any("settled 3 times" in err for err in before.errors), before.errors

    # Park again: an ordinary, unprivileged operation.
    _park(tmp_path, chain, "T-relaunder", wt)

    after = verify_suspension_continuity(sdd_dir=sdd, task_id="T-relaunder", chain=chain)
    assert after.status == CONTINUITY_FAILED, after.to_dict()
    assert any("settled 3 times" in err for err in after.errors), after.errors


def test_writer_and_verifier_agree_that_a_half_matching_receipt_is_evidence(tmp_path: Path) -> None:
    """A receipt matching one identifier but not the other must not vanish.

    The guard treats such a receipt as a settlement and refuses to resume
    forever. If the proof ignores it, an operator sees a healthy 'pending' for
    a park that can never move again, and a real replay attempt leaves no
    trace in the proof at all.
    """
    from bernstein.core.security.audit_chain import record_task_resume

    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-half", wt)

    # Genuine receipt hash, but names a suspend row that is not this park's.
    record_task_resume(
        chain=chain,
        task_id="T-half",
        suspend_receipt_hash=park.suspend_receipt_hash,
        suspend_event_hash="0" * 64,
        resume_event_hash="0" * 64,
        journal_index=99,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=park.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash="0" * 64,
    )

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-half", chain=chain)
    # The verifier surfaces it rather than reporting a clean pending...
    assert result.status == CONTINUITY_FAILED, result.to_dict()
    assert any("inconsistent" in err for err in result.errors), result.errors
    # ...and the writer agrees it is a settlement.
    with pytest.raises(SuspensionAlreadySettledError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=park.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=park.suspend_receipt_hash,
        )


def test_settlement_survives_deletion_of_the_audit_chain_tail(tmp_path: Path) -> None:
    """The guard consults the journal too, not only the audit chain.

    HMAC chaining detects modification and removal of a *non-terminal* entry,
    so dropping the last line of the audit file leaves ``chain.verify()``
    returning True while erasing the receipt. The journal independently
    recorded the settlement, so the replay is still refused.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = park_task(
        sdd_dir=sdd,
        task_id="T-tail",
        adapter="claude",
        session_id="s",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=0.0,
        chain=chain,
        wake_condition=WAKE_APPROVAL,
    )
    approvals = sdd / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    (approvals / "T-tail.approved").write_text("approved", encoding="utf-8")
    approval_ref = approval_decision_ref(tmp_path, "T-tail")
    resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        approval_ref=approval_ref,
    )
    journal_resumes = sum(1 for r in _journal_rows(sdd, "T-tail") if r.get("event") == JOURNAL_EVENT_RESUME)
    assert journal_resumes == 1

    # Drop the terminal audit entry; the chain still verifies.
    log = sorted((tmp_path / "audit").glob("*.jsonl"))[0]
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    reopened = AuditChainStore(tmp_path / "audit", key=_KEY)
    assert reopened.verify()[0]
    assert reopened.query(event_type=EVENT_TASK_RESUMED) == []

    with pytest.raises(SuspensionAlreadySettledError):
        resume_task(
            sdd_dir=sdd,
            suspend_row=park.suspend_row,
            new_worktree_path=wt,
            chain=reopened,
            suspend_receipt_hash=park.suspend_receipt_hash,
            approval_ref=approval_ref,
        )
    # No second resume row landed.
    assert sum(1 for r in _journal_rows(sdd, "T-tail") if r.get("event") == JOURNAL_EVENT_RESUME) == 1


def test_find_settlements_reads_both_stores(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    park = _park(tmp_path, chain, "T-both", wt)
    assert (
        find_settlements(
            sdd_dir=sdd,
            task_id="T-both",
            chain=chain,
            suspend_receipt_hash=park.suspend_receipt_hash,
            suspend_event_hash=park.suspend_row.event_hash,
        )
        == []
    )
    resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    found = find_settlements(
        sdd_dir=sdd,
        task_id="T-both",
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
        suspend_event_hash=park.suspend_row.event_hash,
    )
    assert {s.source for s in found} == {"chain", "journal"}
    assert all(s.consistent for s in found)


@pytest.mark.parametrize("command_name", ["approve", "reject"])
def test_decision_commands_refuse_a_traversing_task_id(tmp_path: Path, command_name: str) -> None:
    """The write side of the approvals sink obeys the same rule as the read side."""
    from click.testing import CliRunner

    from bernstein.cli.commands.approve_cmd import approve
    from bernstein.cli.commands.reject_cmd import reject

    proj = tmp_path / "proj"
    (proj / ".sdd").mkdir(parents=True)
    command = approve if command_name == "approve" else reject
    args = ["../../../../pwned", "--workdir", str(proj)]
    if command_name == "approve":
        args.append("--no-prompt")

    result = CliRunner().invoke(command, args)
    assert result.exit_code == 1, result.output
    assert "refusing" in " ".join(result.output.split()).lower()
    # Nothing was written anywhere outside the approvals directory.
    assert list(tmp_path.rglob("pwned.*")) == []


def test_decision_commands_still_work_for_ordinary_task_ids(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.cli.commands.approve_cmd import approve

    proj = tmp_path / "proj"
    (proj / ".sdd").mkdir(parents=True)
    result = CliRunner().invoke(approve, ["T-ok_1.2", "--workdir", str(proj), "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert (proj / ".sdd" / "runtime" / "approvals" / "T-ok_1.2.approved").exists()


# ---------------------------------------------------------------------------
# Convergence: every entry point must reach the SAME rule. These tests fail if
# any sink grows its own copy of the identifier rule or of "settled", which is
# the drift that produced the findings above.
# ---------------------------------------------------------------------------

#: Every id that must be refused, and every id that must be accepted, by every
#: approvals sink. One table, asserted against all entry points, so a sink
#: cannot quietly diverge.
_UNSAFE_IDS = [
    "../../../../pwned",
    "..",
    ".",
    "a/b",
    "/abs",
    "with space",
    "",
    "back\\slash",
    "auth:oauth-flow",  # colon: drive-relative on Windows, ADS on NTFS
    "T" + "a" * 64,  # 65 chars: over the shared 64 bound
]
_SAFE_IDS = ["T-abc123", "T_1.2-x", "T" + "a" * 63]


def _approvals_sinks() -> dict[str, Any]:
    """Return ``{name: callable(workdir, task_id)}`` for every approvals sink.

    Any new sink under ``.sdd/runtime/approvals`` belongs in this table.
    """
    from click.testing import CliRunner

    from bernstein.cli.commands.approve_cmd import approve
    from bernstein.cli.commands.chat_cmd import _write_approval_decision
    from bernstein.cli.commands.reject_cmd import reject
    from bernstein.core.orchestration.approval_gate import approval_path

    def _cli(command: Any, extra: list[str]) -> Any:
        def run(workdir: Path, task_id: str) -> None:
            result = CliRunner().invoke(command, [task_id, "--workdir", str(workdir), *extra])
            if result.exit_code != 0:
                raise UnsafeTaskIdError(result.output)

        return run

    from bernstein.core.orchestration.approval_gate import approval_path_in

    return {
        "approval_gate.approval_path": lambda wd, tid: approval_path(wd, tid, ".approved"),
        "approval_gate.approval_path_in": lambda wd, tid: approval_path_in(
            wd / ".sdd" / "runtime" / "approvals", tid, ".approved"
        ),
        "suspension.approval_decision_ref": approval_decision_ref,
        "suspension.write_resume_marker": lambda wd, tid: write_resume_marker(wd, tid, "hash"),
        "cli.approve": _cli(approve, ["--no-prompt"]),
        "cli.reject": _cli(reject, []),
        "chat._write_approval_decision": lambda wd, tid: _write_approval_decision(wd, tid, "approve"),
    }


@pytest.mark.parametrize("unsafe_id", _UNSAFE_IDS)
def test_every_approvals_sink_refuses_the_same_unsafe_ids(tmp_path: Path, unsafe_id: str) -> None:
    """One identifier rule, enforced at every entry point.

    Finding: the read side was hardened and its write-side siblings were not,
    so the documented invariant was false. This asserts the rule at all of
    them against one table, so hardening one sink can no longer leave another
    behind.
    """
    for name, sink in _approvals_sinks().items():
        workdir = tmp_path / f"wd-{abs(hash(name)) % 10000}"
        (workdir / ".sdd").mkdir(parents=True, exist_ok=True)
        with pytest.raises(UnsafeTaskIdError):
            sink(workdir, unsafe_id)
        # Nothing escaped, at any sink.
        assert list(tmp_path.rglob("pwned*")) == []


@pytest.mark.parametrize("safe_id", _SAFE_IDS)
def test_every_approvals_sink_accepts_the_same_safe_ids(tmp_path: Path, safe_id: str) -> None:
    """The shared rule must not be so strict that ordinary ids break."""
    for name, sink in _approvals_sinks().items():
        workdir = tmp_path / f"wd-{abs(hash(name)) % 10000}"
        (workdir / ".sdd" / "runtime" / "approvals").mkdir(parents=True, exist_ok=True)
        sink(workdir, safe_id)  # must not raise


def test_writer_and_proof_agree_on_settled_for_every_shape(tmp_path: Path) -> None:
    """One definition of settled, shared by the mutation path and the proof.

    For each way a settlement can be recorded, the writer's answer ("is this
    park spent?") and the proof's answer ("does this park report a clean
    continuity?") must not contradict each other. The findings were exactly
    the cases where they did.
    """
    from bernstein.core.security.audit_chain import record_task_resume

    def fresh(name: str) -> tuple[Path, AuditChainStore, Path, Any]:
        base = tmp_path / name
        base.mkdir()
        wt = _worktree(base, "wt", {"a.py": "x = 1\n"})
        chain = AuditChainStore(base / "audit", key=_KEY)
        park = park_task(
            sdd_dir=base / ".sdd",
            task_id=name,
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=0.0,
            chain=chain,
        )
        return base / ".sdd", chain, wt, park

    def writer_says_spent(sdd: Path, chain: AuditChainStore, wt: Path, park: Any) -> bool:
        try:
            resume_task(
                sdd_dir=sdd,
                suspend_row=park.suspend_row,
                new_worktree_path=wt,
                chain=chain,
                suspend_receipt_hash=park.suspend_receipt_hash,
            )
        except SuspensionAlreadySettledError:
            return True
        return False

    # Shape A: untouched park. Writer: not spent. Proof: pending, no errors.
    sdd, chain, wt, park = fresh("shape-a")
    proof = verify_suspension_continuity(sdd_dir=sdd, task_id="shape-a", chain=chain)
    assert proof.status == CONTINUITY_PENDING
    assert not writer_says_spent(sdd, chain, wt, park)

    # Shape B: genuinely settled. Writer: spent. Proof: verified.
    sdd, chain, wt, park = fresh("shape-b")
    resume_task(
        sdd_dir=sdd,
        suspend_row=park.suspend_row,
        new_worktree_path=wt,
        chain=chain,
        suspend_receipt_hash=park.suspend_receipt_hash,
    )
    assert verify_suspension_continuity(sdd_dir=sdd, task_id="shape-b", chain=chain).status == CONTINUITY_VERIFIED
    assert writer_says_spent(sdd, chain, wt, park)

    # Shape C: receipt matches the park's receipt but names another row.
    # Writer: spent. Proof must NOT report a clean pending.
    sdd, chain, wt, park = fresh("shape-c")
    record_task_resume(
        chain=chain,
        task_id="shape-c",
        suspend_receipt_hash=park.suspend_receipt_hash,
        suspend_event_hash="0" * 64,
        resume_event_hash="0" * 64,
        journal_index=99,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=park.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash="0" * 64,
    )
    assert verify_suspension_continuity(sdd_dir=sdd, task_id="shape-c", chain=chain).status == CONTINUITY_FAILED
    assert writer_says_spent(sdd, chain, wt, park)

    # Shape D: receipt names the park's row but hangs off another receipt.
    # Writer: spent. Proof must NOT report a clean pending.
    sdd, chain, wt, park = fresh("shape-d")
    record_task_resume(
        chain=chain,
        task_id="shape-d",
        suspend_receipt_hash="f" * 64,
        suspend_event_hash=park.suspend_row.event_hash,
        resume_event_hash="0" * 64,
        journal_index=99,
        effective_mode="warm",
        requested_mode="warm",
        workspace_match=True,
        new_workspace_hash=park.suspend_row.workspace_hash,
        downgrade_reason="",
        decision_hash="0" * 64,
    )
    assert verify_suspension_continuity(sdd_dir=sdd, task_id="shape-d", chain=chain).status == CONTINUITY_FAILED
    assert writer_says_spent(sdd, chain, wt, park)


# ---------------------------------------------------------------------------
# Cross-boundary id rules: pin the RELATIONSHIP between the approvals rule and
# the artifact-posting rule, not their current literal values.
# ---------------------------------------------------------------------------

#: Ids that ``evidence.run_artifacts`` accepts but the approvals sink refuses,
#: each with the reason it cannot be admitted. Anything accepted by
#: run_artifacts and not listed here MUST be accepted by the approvals sink.
#:
#: These are deliberate divergences, not oversights. A colon cannot be made
#: safe for a path that is joined then written: on Windows ``C:evil`` parses as
#: drive-relative so the join discards the base, and ``file:stream`` addresses
#: an NTFS alternate data stream that a containment check cannot see. The
#: length and leading-character cases are the shared rule being stricter than a
#: surface that never joins the value onto a directory.
_DELIBERATE_DIVERGENCES = {
    "colon is a drive separator and an NTFS ADS separator",
    "longer than the 64-character identifier budget",
    "does not start with an alphanumeric (would admit '.' and '..')",
}


def _divergence_reason(candidate: str) -> str | None:
    """Return why the approvals rule refuses an id run_artifacts accepts."""
    if ":" in candidate:
        return "colon is a drive separator and an NTFS ADS separator"
    if len(candidate) > 64:
        return "longer than the 64-character identifier budget"
    if not candidate[:1].isalnum():
        return "does not start with an alphanumeric (would admit '.' and '..')"
    return None


def test_every_artifact_accepted_id_is_approvable_or_a_documented_divergence(tmp_path: Path) -> None:
    """Pin the relationship between the two id rules.

    ``evidence.run_artifacts`` accepts a wider alphabet than the approvals
    sink. Every id it admits must therefore be either approvable or a
    *documented* refusal -- otherwise a task can post artifacts but can never
    be approved or rejected, which strands it with no operator remedy.
    """
    from bernstein.core.evidence.run_artifacts import _TASK_ID_RE as ARTIFACT_ID_RE
    from bernstein.core.orchestration.approval_gate import approval_path_in

    approvals = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)

    candidates = [
        "T-abc123",
        "T_1.2-x",
        "task.with.dots",
        "UPPER-lower_9",
        "a",
        "T" + "a" * 63,  # 64: the shared bound
        "auth:oauth-flow",  # accepted by run_artifacts, refused here
        "C:evil",
        "file:stream",
        "T" + "a" * 64,  # 65: over the shared bound
        ".hidden",  # leading dot
        "T" + "a" * 255,  # 256: run_artifacts' own ceiling
    ]

    for candidate in candidates:
        if not ARTIFACT_ID_RE.match(candidate):
            continue  # run_artifacts refuses it too; no relationship to pin
        reason = _divergence_reason(candidate)
        if reason is None:
            # run_artifacts accepts it and there is no documented reason to
            # refuse, so the approvals sink must accept it.
            approval_path_in(approvals, candidate, ".approved")
        else:
            assert reason in _DELIBERATE_DIVERGENCES, f"undocumented divergence for {candidate!r}: {reason}"
            with pytest.raises(UnsafeTaskIdError):
                approval_path_in(approvals, candidate, ".approved")


def test_approvals_rule_matches_the_prevailing_identifier_length(tmp_path: Path) -> None:
    """The shared bound tracks the codebase's prevailing rule, not a magic number.

    ``replay.journal`` and ``run_service.paths`` both cap identifiers at 64. If
    one of those moves, this fails rather than silently leaving the approvals
    sink stricter and stranding ids the rest of the tree accepts.
    """
    import re as _re

    from bernstein.core.orchestration.approval_gate import approval_path_in
    from bernstein.core.replay.journal import _RUN_ID_RE
    from bernstein.core.run_service.paths import _RUN_ID_RE as PATHS_RUN_ID_RE

    def _upper_bound(pattern: _re.Pattern[str]) -> int:
        match = _re.search(r"\{\d+,(\d+)\}", pattern.pattern)
        assert match, f"cannot read bound from {pattern.pattern!r}"
        return int(match.group(1))

    prevailing = {_upper_bound(_RUN_ID_RE), _upper_bound(PATHS_RUN_ID_RE)}
    assert prevailing == {64}, prevailing

    approvals = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    approval_path_in(approvals, "T" + "a" * 63, ".approved")  # 64: accepted
    with pytest.raises(UnsafeTaskIdError):
        approval_path_in(approvals, "T" + "a" * 64, ".approved")  # 65: refused


def test_a_task_too_long_to_park_is_still_approvable(tmp_path: Path) -> None:
    """The narrower park budget must not strand a task at the approvals surface.

    A 60-64 character id cannot be durably parked (the journal run id would
    exceed 64), but it must still be approvable and rejectable, and the park
    refusal must be the typed one rather than a bare ValueError from the
    journal.
    """
    from bernstein.core.orchestration.approval_gate import approval_path_in

    long_id = "T" + "a" * 62  # 63 chars: fine for approvals, too long to park
    approvals = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)

    # Approvable.
    assert approval_path_in(approvals, long_id, ".approved").parent == approvals.resolve()

    # Not parkable, with the typed refusal.
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    with pytest.raises(UnsafeTaskIdError):
        park_task(
            sdd_dir=tmp_path / ".sdd",
            task_id=long_id,
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=1.0,
            spent_usd=0.0,
            chain=chain,
        )


def test_colon_id_escapes_containment_under_windows_semantics() -> None:
    """Evidence for the colon divergence, so the reason is checkable not asserted.

    This is why a colon cannot simply be admitted to match ``run_artifacts``.
    Uses pure path semantics so it holds on any host.
    """
    from pathlib import PureWindowsPath

    base = PureWindowsPath(r"D:\proj\.sdd\runtime\approvals")
    # A drive-letter shaped id discards the base entirely on Windows.
    escaped = base / "C:evil.approved"
    assert not escaped.is_relative_to(base)
    assert str(escaped) == "C:evil.approved"
    # An ADS-shaped id stays "contained" by path inspection, which is exactly
    # why a containment check alone cannot make a colon safe.
    ads = base / "file:stream.approved"
    assert ads.is_relative_to(base)
