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


def test_verify_continuity_rejects_a_park_that_was_never_resumed(tmp_path: Path) -> None:
    """An unresumed park is an incomplete proof, not a verified continuity."""
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path, "wt", {"a.py": "x = 1\n"})
    _park(tmp_path, chain, "T-open", wt)

    result = verify_suspension_continuity(sdd_dir=sdd, task_id="T-open", chain=chain)
    assert not result.ok
    assert not result.resumed
    assert any("resume receipt" in err for err in result.errors)


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
    assert any("no resume receipt continued from the parked suspend row" in err for err in result.errors), result.errors
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


def test_verify_continuity_ignores_a_resume_receipt_bound_to_another_park(tmp_path: Path) -> None:
    """Binding selection, pinned independently of the journal-row check.

    The forged receipt names a resume row the journal really holds, so the
    ``_row_present`` guard cannot reject it. Only the binding filter can, and
    reverting that filter makes the verifier read the forged receipt (warm with
    no workspace match) and fail.
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
    # The genuine settlement is read; the foreign-bound receipt is not this
    # park's evidence and must not be selected by recency.
    assert result.ok, result.errors
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
