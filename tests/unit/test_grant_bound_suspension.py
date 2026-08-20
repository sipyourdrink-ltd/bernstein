"""Tests for issue #3649 — suspend-side grant population and journal append.

Remaining acceptance criteria:
    AC-SUSP  park_task() populates role, grant_hash, parent_run_id,
             chain_head_at_suspend on AgentCheckpoint at suspend time.
    AC-CONT  resume_task() appends a task.grant_continuation row to the journal
             binding (checkpoint_hash, grant_hash, chain_head_at_suspend,
             chain_head_at_resume) immediately after the resume row is written.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.persistence.agent_checkpoint import (
    compute_grant_hash,
    find_checkpoint_for_task,
)
from bernstein.core.replay.journal import load_events
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.permissions import get_permissions_for_role
from bernstein.core.tasks.suspension import (
    JOURNAL_EVENT_GRANT_CONTINUATION,
    SuspendRow,
    park_task,
    resume_task,
)

_KEY = b"test-key-32-bytes-exactly-------"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _worktree(tmp_path: Path, name: str = "wt") -> Path:
    wt = tmp_path / name
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "work.py").write_text("# in progress\n", encoding="utf-8")
    return wt


def _park(
    tmp_path: Path,
    *,
    task_id: str = "T-grant",
    role: str = "backend",
    parent_run_id: str = "run-42",
) -> tuple[SuspendRow, str, AuditChainStore]:
    """Park a task with a grant-bound checkpoint.

    ``park_task`` is the writer of the checkpoint, so the role and the owning
    run are handed to it rather than pre-seeded on disk: a checkpoint written
    before the park carries no suspend-row hash to bind the grant to.

    Returns ``(suspend_row, suspend_receipt_hash, chain)``.
    """
    sdd = tmp_path / ".sdd"
    chain = _chain(tmp_path)
    wt = _worktree(tmp_path)

    result = park_task(
        sdd_dir=sdd,
        task_id=task_id,
        adapter="claude",
        session_id="sess-1",
        worktree_path=wt,
        envelope="subscription",
        reserved_usd=5.0,
        spent_usd=1.0,
        chain=chain,
        role=role,
        parent_run_id=parent_run_id,
    )
    return result.suspend_row, result.suspend_receipt_hash, chain


# ---------------------------------------------------------------------------
# AC-SUSP: suspend-side population of AgentCheckpoint grant fields
# ---------------------------------------------------------------------------


class TestSuspendSideGrantPopulation:
    def test_park_writes_grant_hash_to_checkpoint(self, tmp_path: Path) -> None:
        """park_task() must populate grant_hash on the stored AgentCheckpoint."""
        _park(tmp_path, task_id="T-susp-grant")

        updated = find_checkpoint_for_task("T-susp-grant", tmp_path / ".sdd" / "runtime")
        assert updated is not None
        assert updated.grant_hash != "", "grant_hash must be populated after park"

    def test_park_grant_hash_is_correct(self, tmp_path: Path) -> None:
        """The stored grant_hash must match compute_grant_hash() output."""
        task_id = "T-hash-correct"
        role = "backend"
        parent_run_id = "run-99"
        _park(tmp_path, task_id=task_id, role=role, parent_run_id=parent_run_id)

        updated = find_checkpoint_for_task(task_id, tmp_path / ".sdd" / "runtime")
        assert updated is not None

        expected = compute_grant_hash(
            role,
            get_permissions_for_role(role),
            task_id,
            parent_run_id,
            updated.chain_head_at_suspend,
        )
        assert updated.grant_hash == expected

    def test_park_populates_chain_head_at_suspend(self, tmp_path: Path) -> None:
        """chain_head_at_suspend must equal the suspend row's event_hash."""
        task_id = "T-chain-head"
        suspend_row, _receipt_hash, _chain_store = _park(tmp_path, task_id=task_id)

        updated = find_checkpoint_for_task(task_id, tmp_path / ".sdd" / "runtime")
        assert updated is not None
        assert updated.chain_head_at_suspend == suspend_row.event_hash

    def test_park_populates_role_and_parent_run_id(self, tmp_path: Path) -> None:
        """role and parent_run_id must reach the checkpoint the resume re-derives from."""
        task_id = "T-role-parent"
        _park(tmp_path, task_id=task_id, role="qa", parent_run_id="run-777")

        updated = find_checkpoint_for_task(task_id, tmp_path / ".sdd" / "runtime")
        assert updated is not None
        assert updated.role == "qa"
        assert updated.parent_run_id == "run-777"

    def test_park_without_a_role_writes_no_grant(self, tmp_path: Path) -> None:
        """A park that cannot source a role writes an empty grant, and does not fail.

        ``get_permissions_for_role("")`` is the *unrestricted* permission set,
        which the resume would re-derive identically -- so hashing it would
        produce a checkpoint that reads as grant-bound and can never refuse.
        Absence has to stay absence.
        """
        sdd = tmp_path / ".sdd"
        chain = _chain(tmp_path)
        wt = _worktree(tmp_path)

        result = park_task(
            sdd_dir=sdd,
            task_id="T-no-role",
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=1.0,
            chain=chain,
        )
        assert result.suspend_row.task_id == "T-no-role"

        written = find_checkpoint_for_task("T-no-role", sdd / "runtime")
        assert written is not None
        assert written.role == ""
        assert written.grant_hash == ""


# ---------------------------------------------------------------------------
# AC-CONT: journal append of ContinuationEntry on successful resume
# ---------------------------------------------------------------------------


class TestJournalContinuationEntry:
    def test_resume_appends_grant_continuation_row(self, tmp_path: Path) -> None:
        """resume_task() appends a task.grant_continuation row to the journal."""
        suspend_row, receipt_hash, chain = _park(tmp_path)
        sdd = tmp_path / ".sdd"
        wt2 = _worktree(tmp_path, "wt2")

        resume_task(
            sdd_dir=sdd,
            suspend_row=suspend_row,
            new_worktree_path=wt2,
            chain=chain,
            suspend_receipt_hash=receipt_hash,
        )

        from bernstein.core.tasks.checkpoint_retry import task_run_id

        journal_path = sdd / "runs" / task_run_id(suspend_row.task_id) / "journal.jsonl"
        events = load_events(journal_path).events
        continuation_rows = [e for e in events if e.get("event") == JOURNAL_EVENT_GRANT_CONTINUATION]
        assert len(continuation_rows) == 1, "Exactly one grant_continuation row must be appended on successful resume"

    def test_continuation_row_binds_correct_fields(self, tmp_path: Path) -> None:
        """The continuation row must bind checkpoint_hash, grant_hash,
        chain_head_at_suspend, chain_head_at_resume."""
        task_id = "T-cont-fields"
        suspend_row, receipt_hash, chain = _park(tmp_path, task_id=task_id)
        sdd = tmp_path / ".sdd"
        wt2 = _worktree(tmp_path, "wt2")

        result = resume_task(
            sdd_dir=sdd,
            suspend_row=suspend_row,
            new_worktree_path=wt2,
            chain=chain,
            suspend_receipt_hash=receipt_hash,
        )

        from bernstein.core.tasks.checkpoint_retry import task_run_id

        journal_path = sdd / "runs" / task_run_id(task_id) / "journal.jsonl"
        events = load_events(journal_path).events
        row = next(e for e in events if e.get("event") == JOURNAL_EVENT_GRANT_CONTINUATION)

        assert row["chain_head_at_resume"] == result.resume_event_hash
        assert row["chain_head_at_suspend"] == suspend_row.event_hash
        # grant_hash and checkpoint_hash must be non-empty strings
        assert isinstance(row.get("grant_hash"), str)
        assert isinstance(row.get("checkpoint_hash"), str)

    def test_continuation_row_appears_after_resume_row(self, tmp_path: Path) -> None:
        """grant_continuation must come after the task.resume row in the journal."""
        suspend_row, receipt_hash, chain = _park(tmp_path)
        sdd = tmp_path / ".sdd"
        wt2 = _worktree(tmp_path, "wt2")

        resume_task(
            sdd_dir=sdd,
            suspend_row=suspend_row,
            new_worktree_path=wt2,
            chain=chain,
            suspend_receipt_hash=receipt_hash,
        )

        from bernstein.core.tasks.checkpoint_retry import task_run_id

        journal_path = sdd / "runs" / task_run_id(suspend_row.task_id) / "journal.jsonl"
        events = load_events(journal_path).events
        event_types = [e.get("event") for e in events]

        resume_idx = next(i for i, e in enumerate(event_types) if e == "task.resume")
        cont_idx = next(i for i, e in enumerate(event_types) if e == JOURNAL_EVENT_GRANT_CONTINUATION)
        assert cont_idx > resume_idx

    def test_resume_receipt_journal_index_names_the_resume_row(self, tmp_path: Path) -> None:
        """The receipt's journal_index and resume_event_hash must name the same row.

        The continuation row is appended between the resume append and the
        receipt, so an index read afterwards names the continuation row while
        ``resume_event_hash`` still names the resume row -- one signed receipt
        describing two. The suspend side already refuses exactly that mismatch
        (``verify_suspension_receipt``), so the resume side must not write one.
        """
        from bernstein.core.security.audit_chain import EVENT_TASK_RESUMED
        from bernstein.core.tasks.checkpoint_retry import task_run_id

        task_id = "T-resume-index"
        suspend_row, receipt_hash, chain = _park(tmp_path, task_id=task_id)
        sdd = tmp_path / ".sdd"
        wt2 = _worktree(tmp_path, "wt2")

        result = resume_task(
            sdd_dir=sdd,
            suspend_row=suspend_row,
            new_worktree_path=wt2,
            chain=chain,
            suspend_receipt_hash=receipt_hash,
        )

        journal_path = sdd / "runs" / task_run_id(task_id) / "journal.jsonl"
        events = load_events(journal_path).events
        # The continuation row must actually be there, or this proves nothing.
        assert any(e.get("event") == JOURNAL_EVENT_GRANT_CONTINUATION for e in events)

        scan = chain.scan_verified(event_type=EVENT_TASK_RESUMED)
        assert scan.ok, scan.errors
        receipt = next(e for e in scan.events if e.details.get("task_id") == task_id)
        index = int(receipt.details["journal_index"])

        assert events[index].get("event") == "task.resume"
        assert receipt.details["resume_event_hash"] == result.resume_event_hash

    def test_no_continuation_row_when_the_checkpoint_carries_no_grant(self, tmp_path: Path) -> None:
        """A checkpoint with no grant produces no continuation evidence.

        Continuation is a claim about authority, so it is written only when
        there is an authority to name. A park with no role -- and a task old
        enough to have no checkpoint at all -- both resume normally and simply
        leave no continuation row, which the verifier reads as a new run.
        """
        sdd = tmp_path / ".sdd"
        chain = _chain(tmp_path)
        wt = _worktree(tmp_path)

        # Park with no role: the checkpoint is written with an empty grant_hash.
        result = park_task(
            sdd_dir=sdd,
            task_id="T-no-cp",
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=1.0,
            chain=chain,
        )
        wt2 = _worktree(tmp_path, "wt2")

        resume_task(
            sdd_dir=sdd,
            suspend_row=result.suspend_row,
            new_worktree_path=wt2,
            chain=chain,
            suspend_receipt_hash=result.suspend_receipt_hash,
        )

        from bernstein.core.tasks.checkpoint_retry import task_run_id

        journal_path = sdd / "runs" / task_run_id("T-no-cp") / "journal.jsonl"
        events = load_events(journal_path).events
        continuation_rows = [e for e in events if e.get("event") == JOURNAL_EVENT_GRANT_CONTINUATION]
        assert continuation_rows == [], "No continuation row should appear when the checkpoint carries no grant"
