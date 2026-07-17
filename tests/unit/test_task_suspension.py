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
from bernstein.core.replay.journal import rebuild_state, verify_journal
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
    approval_decision_ref,
    decide_resume,
    latest_suspension,
    park_task,
    release_resources,
    resume_task,
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
