"""Tests for issue #3649 — a checkpoint binds the observations it depended on.

The grant binding (already shipped) answers "may this run continue?". These
tests cover the second question a checkpoint could not answer: "are the bytes
the suspended work was derived from still the bytes on disk?".

The outcome differs from the grant case on purpose. A moved grant *refuses* the
resume. A moved observation makes the checkpoint a **discard candidate**:
respawning from scratch is the expected answer, and resuming onto changed bytes
while recording it as continuity is the thing that must not happen.

Numbered properties:
     1. capture binds content, not path identity
     2. capture is bounded to the uncommitted set (states what is not bound)
     3. unchanged observations are not a discard candidate
     4. a changed artifact is a discard candidate naming the artifact
     5. a deleted artifact is a discard candidate naming the artifact
     6. delete-then-recreate with identical bytes is NOT a discard candidate
     7. the verdict says respawning is the expected answer
     8. a moved observation is not turned into a grant refusal
     9. a moved observation stops the resume before any side effect
    10. --override-observations lets the resume proceed
    11. the override is recorded on the continuation entry
    12. discarding drops the checkpoint so the run is not a continuation
    13. park_task binds the uncommitted worktree bytes in production
    14. a checkpoint carrying no observations is not a discard candidate
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.cli.commands.resume_cmd import (
    EXIT_OBSERVATIONS_MOVED,
    ObservationsMovedError,
    discard_agent_checkpoint,
    prepare_resume,
    resume_cmd,
)
from bernstein.core.persistence.agent_checkpoint import (
    AgentCheckpoint,
    build_continuation_entry,
    capture_worktree_observations,
    evaluate_observations,
    is_checkpoint_recoverable,
)
from bernstein.core.persistence.agent_checkpoint import (
    save_checkpoint as save_agent_checkpoint,
)
from bernstein.core.persistence.task_resume import (
    TaskResumeCheckpoint,
    load_checkpoint,
    save_checkpoint,
)
from bernstein.core.replay.journal import load_events
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.tasks.checkpoint_retry import task_run_id
from bernstein.core.tasks.suspension import (
    JOURNAL_EVENT_GRANT_CONTINUATION,
    park_task,
    resume_task,
)

_KEY = b"test-key-32-bytes-exactly-------"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(worktree: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True)


def _init_worktree(path: Path) -> Path:
    """A git worktree with one committed file and one uncommitted edit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "gc.auto", "0")
    _git(path, "config", "maintenance.auto", "false")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "schema.sql").write_text("CREATE TABLE t (id INT);\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    (path / "work.py").write_text("# in progress\n", encoding="utf-8")
    return path


def _agent_checkpoint(worktree: Path, *, task_id: str = "t-obs") -> AgentCheckpoint:
    return AgentCheckpoint(
        agent_id=f"agent-{task_id}",
        task_id=task_id,
        worktree_path=str(worktree),
        observations=capture_worktree_observations(worktree),
    )


def _task_checkpoint(task_id: str) -> TaskResumeCheckpoint:
    return TaskResumeCheckpoint(
        task_id=task_id,
        last_completed_step_id="step-1",
        trace_cursor=0,
        scratchpad_path=None,
        adapter="claude",
        adapter_session_id="sess-1",
        worktree_path="/tmp/wt",
    )


def _seed_resume(workdir: Path, task_id: str, worktree: Path) -> AgentCheckpoint:
    """Both checkpoints ``bernstein resume`` reads, on disk."""
    save_checkpoint(workdir, _task_checkpoint(task_id))
    cp = _agent_checkpoint(worktree, task_id=task_id)
    save_agent_checkpoint(cp, workdir / ".sdd" / "runtime")
    return cp


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


# ---------------------------------------------------------------------------
# 1-2  capture_worktree_observations
# ---------------------------------------------------------------------------


class TestCapture:
    def test_capture_binds_content_not_path_identity(self, tmp_path: Path) -> None:
        """1. The same bytes hash the same however the file got there."""
        wt = _init_worktree(tmp_path / "wt")
        before = capture_worktree_observations(wt)

        (wt / "work.py").unlink()
        (wt / "work.py").write_text("# in progress\n", encoding="utf-8")
        after = capture_worktree_observations(wt)

        assert before == after
        assert before["work.py"].startswith("sha256:")

    def test_capture_is_bounded_to_the_uncommitted_set(self, tmp_path: Path) -> None:
        """2. Committed, untouched files are not bound.

        What a checkpoint can bind without inferring a dependency set is the
        work it is carrying. ``schema.sql`` is committed and unmodified, so it
        is deliberately outside the binding.
        """
        wt = _init_worktree(tmp_path / "wt")
        observations = capture_worktree_observations(wt)

        assert "work.py" in observations
        assert "schema.sql" not in observations

    def test_capture_on_a_non_git_directory_binds_nothing(self, tmp_path: Path) -> None:
        """A directory git cannot describe yields no false observations."""
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("a\n", encoding="utf-8")
        assert capture_worktree_observations(plain) == {}


# ---------------------------------------------------------------------------
# 3-7, 14  evaluate_observations
# ---------------------------------------------------------------------------


class TestEvaluateObservations:
    def test_unchanged_observations_are_not_a_discard_candidate(self, tmp_path: Path) -> None:
        """3."""
        wt = _init_worktree(tmp_path / "wt")
        verdict = evaluate_observations(_agent_checkpoint(wt))

        assert verdict.discard_candidate is False
        assert verdict.moved == ()
        assert verdict.missing == ()

    def test_changed_artifact_is_a_discard_candidate_naming_it(self, tmp_path: Path) -> None:
        """4."""
        wt = _init_worktree(tmp_path / "wt")
        cp = _agent_checkpoint(wt)
        (wt / "work.py").write_text("# somebody else edited this\n", encoding="utf-8")

        verdict = evaluate_observations(cp)

        assert verdict.discard_candidate is True
        assert verdict.moved == ("work.py",)
        assert "work.py" in verdict.reason

    def test_deleted_artifact_is_a_discard_candidate_naming_it(self, tmp_path: Path) -> None:
        """5."""
        wt = _init_worktree(tmp_path / "wt")
        cp = _agent_checkpoint(wt)
        (wt / "work.py").unlink()

        verdict = evaluate_observations(cp)

        assert verdict.discard_candidate is True
        assert verdict.missing == ("work.py",)
        assert "work.py" in verdict.reason

    def test_deleted_and_recreated_identical_content_is_not_a_discard_candidate(self, tmp_path: Path) -> None:
        """6. Load-bearing: separates a content hash from a stat check.

        A worktree rebuilt from scratch changes every inode and mtime while
        the bytes stay identical. An implementation keyed on either would
        discard every checkpoint on such a host.
        """
        wt = _init_worktree(tmp_path / "wt")
        cp = _agent_checkpoint(wt)
        original = (wt / "work.py").read_bytes()
        (wt / "work.py").unlink()
        (wt / "work.py").write_bytes(original)

        verdict = evaluate_observations(cp)

        assert verdict.discard_candidate is False, verdict.reason

    def test_verdict_says_respawning_is_the_expected_answer(self, tmp_path: Path) -> None:
        """7. The message must not read as a permission refusal."""
        wt = _init_worktree(tmp_path / "wt")
        cp = _agent_checkpoint(wt)
        (wt / "work.py").write_text("# moved\n", encoding="utf-8")

        reason = evaluate_observations(cp).reason

        assert "discard" in reason
        assert "respawn" in reason
        assert "grant" not in reason

    def test_checkpoint_without_observations_is_not_a_discard_candidate(self, tmp_path: Path) -> None:
        """14. Nothing bound is not evidence that something moved."""
        wt = _init_worktree(tmp_path / "wt")
        cp = AgentCheckpoint(agent_id="a", task_id="t", worktree_path=str(wt))

        verdict = evaluate_observations(cp)

        assert verdict.discard_candidate is False

    def test_moved_observation_is_not_a_grant_refusal(self, tmp_path: Path) -> None:
        """8. The authority check keeps its own outcome."""
        wt = _init_worktree(tmp_path / "wt")
        cp = _agent_checkpoint(wt)
        (wt / "work.py").write_text("# moved\n", encoding="utf-8")

        ok, reason = is_checkpoint_recoverable(cp)

        assert ok is True, reason
        assert "discard" not in reason


# ---------------------------------------------------------------------------
# 9-12  the operator-facing resume path
# ---------------------------------------------------------------------------


class TestResumeCommand:
    def test_moved_observation_stops_resume_before_any_side_effect(self, tmp_path: Path) -> None:
        """9. No resume_count bump, no resume signal, no hook."""
        workdir = tmp_path / "proj"
        workdir.mkdir()
        wt = _init_worktree(tmp_path / "wt")
        _seed_resume(workdir, "t-side", wt)
        (wt / "work.py").write_text("# moved underneath\n", encoding="utf-8")

        with pytest.raises(ObservationsMovedError) as excinfo:
            prepare_resume(workdir, "t-side")

        assert "work.py" in str(excinfo.value)
        assert load_checkpoint(workdir, "t-side").resume_count == 0
        assert not (workdir / ".sdd" / "runtime" / "resume").exists()

    def test_override_observations_allows_the_resume(self, tmp_path: Path) -> None:
        """10."""
        workdir = tmp_path / "proj"
        workdir.mkdir()
        wt = _init_worktree(tmp_path / "wt")
        _seed_resume(workdir, "t-override", wt)
        (wt / "work.py").write_text("# moved underneath\n", encoding="utf-8")

        plan = prepare_resume(workdir, "t-override", override_observations=True)

        assert plan.checkpoint.resume_count == 1

    def test_cli_exits_with_the_observations_code(self, tmp_path: Path) -> None:
        """9 (CLI surface)."""
        from click.testing import CliRunner

        workdir = tmp_path / "proj"
        workdir.mkdir()
        wt = _init_worktree(tmp_path / "wt")
        _seed_resume(workdir, "t-cli", wt)
        (wt / "work.py").write_text("# moved underneath\n", encoding="utf-8")

        result = CliRunner().invoke(resume_cmd, ["t-cli", "--workdir", str(workdir)])

        assert result.exit_code == EXIT_OBSERVATIONS_MOVED
        assert "work.py" in result.output

    def test_discard_drops_the_checkpoint(self, tmp_path: Path) -> None:
        """12 (first half): the discard path really removes the checkpoint."""
        workdir = tmp_path / "proj"
        workdir.mkdir()
        wt = _init_worktree(tmp_path / "wt")
        _seed_resume(workdir, "t-discard", wt)

        assert discard_agent_checkpoint(workdir, "t-discard") is True
        assert discard_agent_checkpoint(workdir, "t-discard") is False

        plan = prepare_resume(workdir, "t-discard")
        assert plan.checkpoint.resume_count == 1


# ---------------------------------------------------------------------------
# 11  the override is recorded
# ---------------------------------------------------------------------------


class TestContinuationEntryRecordsObservations:
    def test_entry_binds_the_observations_hash(self, tmp_path: Path) -> None:
        wt = _init_worktree(tmp_path / "wt")
        entry = build_continuation_entry(_agent_checkpoint(wt))

        assert entry.observations_hash != ""
        assert entry.observations_overridden is False

    def test_entry_without_observations_binds_nothing(self, tmp_path: Path) -> None:
        cp = AgentCheckpoint(agent_id="a", task_id="t", worktree_path=str(tmp_path))
        assert build_continuation_entry(cp).observations_hash == ""

    def test_override_is_recorded_on_the_entry(self, tmp_path: Path) -> None:
        """11. A later reader tells an overridden resume from a clean one."""
        wt = _init_worktree(tmp_path / "wt")
        entry = build_continuation_entry(_agent_checkpoint(wt), observations_overridden=True)

        assert entry.observations_overridden is True


# ---------------------------------------------------------------------------
# 12-13  suspend/resume in production
# ---------------------------------------------------------------------------


class TestSuspensionWiring:
    def test_park_binds_the_uncommitted_worktree_bytes(self, tmp_path: Path) -> None:
        """13. The capture activates on a real park, not only in tests."""
        from bernstein.core.persistence.agent_checkpoint import find_checkpoint_for_task

        wt = _init_worktree(tmp_path / "wt")
        park_task(
            sdd_dir=tmp_path / ".sdd",
            task_id="T-park-obs",
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=1.0,
            chain=_chain(tmp_path),
            role="backend",
            parent_run_id="run-1",
        )

        written = find_checkpoint_for_task("T-park-obs", tmp_path / ".sdd" / "runtime")
        assert written is not None
        assert "work.py" in written.observations
        assert written.observations["work.py"].startswith("sha256:")

    def test_continuation_row_carries_the_observation_fields(self, tmp_path: Path) -> None:
        """11 (chain surface)."""
        wt = _init_worktree(tmp_path / "wt")
        chain = _chain(tmp_path)
        parked = park_task(
            sdd_dir=tmp_path / ".sdd",
            task_id="T-cont-obs",
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=1.0,
            chain=chain,
            role="backend",
            parent_run_id="run-1",
        )
        resume_task(
            sdd_dir=tmp_path / ".sdd",
            suspend_row=parked.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=parked.suspend_receipt_hash,
            override_observations=True,
        )

        journal_path = tmp_path / ".sdd" / "runs" / task_run_id("T-cont-obs") / "journal.jsonl"
        row = next(e for e in load_events(journal_path).events if e.get("event") == JOURNAL_EVENT_GRANT_CONTINUATION)

        assert row["observations_hash"] != ""
        assert row["observations_overridden"] is True

    def test_discarded_checkpoint_produces_no_continuation_row(self, tmp_path: Path) -> None:
        """12 (second half): the chain reads the resume as a new run."""
        wt = _init_worktree(tmp_path / "wt")
        chain = _chain(tmp_path)
        parked = park_task(
            sdd_dir=tmp_path / ".sdd",
            task_id="T-discard-obs",
            adapter="claude",
            session_id="s",
            worktree_path=wt,
            envelope="subscription",
            reserved_usd=5.0,
            spent_usd=1.0,
            chain=chain,
            role="backend",
            parent_run_id="run-1",
        )
        assert discard_agent_checkpoint(tmp_path, "T-discard-obs") is True

        resume_task(
            sdd_dir=tmp_path / ".sdd",
            suspend_row=parked.suspend_row,
            new_worktree_path=wt,
            chain=chain,
            suspend_receipt_hash=parked.suspend_receipt_hash,
        )

        journal_path = tmp_path / ".sdd" / "runs" / task_run_id("T-discard-obs") / "journal.jsonl"
        rows = [e for e in load_events(journal_path).events if e.get("event") == JOURNAL_EVENT_GRANT_CONTINUATION]
        assert rows == []
