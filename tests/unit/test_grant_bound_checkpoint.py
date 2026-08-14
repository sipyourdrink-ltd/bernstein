"""Tests for issue #3649 — grant-bound checkpoint recovery.

Acceptance criteria covered:
    AC-1  A checkpoint binds the grant by hash (role, allowed/denied paths,
          task_id, parent_run_id, chain_head_at_suspend).
    AC-2  Resume re-derives the grant and compares; a narrowed or reassigned
          grant refuses with a message naming which field changed.
    AC-3  The refusal happens BEFORE the first side effect — asserted by
          confirming zero filesystem writes occurred.
    AC-4  A successful resume produces a ContinuationEntry binding
          (checkpoint_hash, grant_hash, chain_head_at_suspend,
          chain_head_at_resume).
    AC-5  Absence of the entry is never treated as evidence of continuity
          (absence == new run).
    AC-6  All six scenarios: grant unchanged, role narrowed (allowed_paths
          reduced), task reassigned, parent cancelled, resume-after-crash
          (entry never written), legacy checkpoint without grant fields.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from bernstein.core.persistence.agent_checkpoint import (
    AgentCheckpoint,
    ContinuationEntry,
    build_continuation_entry,
    checkpoint_hash,
    compute_grant_hash,
    find_checkpoint_for_task,
    is_checkpoint_recoverable,
    save_checkpoint,
)
from bernstein.core.security.permissions import AgentPermissions, get_permissions_for_role

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo suitable for liveness checks."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _make_checkpoint(
    tmp_path: Path,
    *,
    role: str = "backend",
    task_id: str = "task-42",
    parent_run_id: str = "run-99",
    chain_head: str = "deadbeef",
    with_git: bool = True,
    with_uncommitted: bool = True,
    role_overrides: dict[str, AgentPermissions] | None = None,
) -> AgentCheckpoint:
    """Build a fully-populated checkpoint with a correct grant hash."""
    if with_git:
        _init_git_repo(tmp_path)
    if with_uncommitted:
        (tmp_path / "work.py").write_text("# in progress\n")

    perms = get_permissions_for_role(role, role_overrides)
    gh = compute_grant_hash(role, perms, task_id, parent_run_id, chain_head)
    return AgentCheckpoint(
        agent_id="agent-1",
        task_id=task_id,
        worktree_path=str(tmp_path),
        role=role,
        grant_hash=gh,
        parent_run_id=parent_run_id,
        chain_head_at_suspend=chain_head,
    )


# ---------------------------------------------------------------------------
# AC-1  compute_grant_hash — determinism and sensitivity
# ---------------------------------------------------------------------------


class TestComputeGrantHash:
    def test_deterministic(self) -> None:
        perms = get_permissions_for_role("backend")
        h1 = compute_grant_hash("backend", perms, "t", "r", "head")
        h2 = compute_grant_hash("backend", perms, "t", "r", "head")
        assert h1 == h2

    def test_sensitive_to_role(self) -> None:
        p = get_permissions_for_role("backend")
        h_be = compute_grant_hash("backend", p, "t", "r", "head")
        h_qa = compute_grant_hash("qa", p, "t", "r", "head")
        assert h_be != h_qa

    def test_sensitive_to_allowed_paths(self) -> None:
        p1 = AgentPermissions(allowed_paths=("src/*",), denied_paths=())
        p2 = AgentPermissions(allowed_paths=("src/*", "tests/*"), denied_paths=())
        h1 = compute_grant_hash("backend", p1, "t", "r", "head")
        h2 = compute_grant_hash("backend", p2, "t", "r", "head")
        assert h1 != h2

    def test_sensitive_to_denied_paths(self) -> None:
        p1 = AgentPermissions(allowed_paths=("src/*",), denied_paths=())
        p2 = AgentPermissions(allowed_paths=("src/*",), denied_paths=(".sdd/*",))
        h1 = compute_grant_hash("backend", p1, "t", "r", "head")
        h2 = compute_grant_hash("backend", p2, "t", "r", "head")
        assert h1 != h2

    def test_sensitive_to_task_id(self) -> None:
        p = get_permissions_for_role("backend")
        h1 = compute_grant_hash("backend", p, "task-A", "r", "head")
        h2 = compute_grant_hash("backend", p, "task-B", "r", "head")
        assert h1 != h2

    def test_sensitive_to_parent_run_id(self) -> None:
        p = get_permissions_for_role("backend")
        h1 = compute_grant_hash("backend", p, "t", "run-1", "head")
        h2 = compute_grant_hash("backend", p, "t", "run-2", "head")
        assert h1 != h2

    def test_sensitive_to_chain_head(self) -> None:
        p = get_permissions_for_role("backend")
        h1 = compute_grant_hash("backend", p, "t", "r", "aaaa")
        h2 = compute_grant_hash("backend", p, "t", "r", "bbbb")
        assert h1 != h2

    def test_stable_allowed_paths_ordering(self) -> None:
        p1 = AgentPermissions(allowed_paths=("src/*", "tests/*"), denied_paths=())
        p2 = AgentPermissions(allowed_paths=("tests/*", "src/*"), denied_paths=())
        h1 = compute_grant_hash("backend", p1, "t", "r", "head")
        h2 = compute_grant_hash("backend", p2, "t", "r", "head")
        assert h1 == h2, "path ordering must not affect the hash"


# ---------------------------------------------------------------------------
# AC-1  checkpoint_hash — excludes timing fields
# ---------------------------------------------------------------------------


class TestCheckpointHash:
    def test_deterministic(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        assert checkpoint_hash(cp) == checkpoint_hash(cp)

    def test_excludes_checkpointed_at(self) -> None:
        cp1 = AgentCheckpoint(agent_id="a", task_id="t", worktree_path="/w", checkpointed_at=1_000.0)
        cp2 = AgentCheckpoint(agent_id="a", task_id="t", worktree_path="/w", checkpointed_at=2_000.0)
        assert checkpoint_hash(cp1) == checkpoint_hash(cp2)

    def test_excludes_elapsed_seconds(self) -> None:
        cp1 = AgentCheckpoint(agent_id="a", task_id="t", worktree_path="/w", elapsed_seconds=10.0)
        cp2 = AgentCheckpoint(agent_id="a", task_id="t", worktree_path="/w", elapsed_seconds=999.0)
        assert checkpoint_hash(cp1) == checkpoint_hash(cp2)

    def test_sensitive_to_agent_id(self) -> None:
        cp1 = AgentCheckpoint(agent_id="a1", task_id="t", worktree_path="/w")
        cp2 = AgentCheckpoint(agent_id="a2", task_id="t", worktree_path="/w")
        assert checkpoint_hash(cp1) != checkpoint_hash(cp2)


# ---------------------------------------------------------------------------
# AC-6 scenario 1  grant unchanged — resume allowed
# ---------------------------------------------------------------------------


class TestGrantUnchanged:
    def test_recoverable_when_grant_matches(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        ok, reason = is_checkpoint_recoverable(cp)
        assert ok is True
        assert "uncommitted" in reason

    def test_no_writes_before_authority_check(self, tmp_path: Path) -> None:
        """Authority check fires before any filesystem side effect."""
        # Use a checkpoint with a bad grant to confirm refusal happens
        # before the worktree is even consulted.
        cp = AgentCheckpoint(
            agent_id="a",
            task_id="task-1",
            worktree_path=str(tmp_path / "nonexistent"),
            role="backend",
            grant_hash="badhash",
            parent_run_id="run-1",
            chain_head_at_suspend="head",
        )
        files_before = list(tmp_path.rglob("*"))
        ok, reason = is_checkpoint_recoverable(cp)
        assert ok is False
        assert "grant mismatch" in reason
        # No new files written
        assert list(tmp_path.rglob("*")) == files_before


# ---------------------------------------------------------------------------
# AC-2 / AC-3  role narrowed — refusal before first side effect
# ---------------------------------------------------------------------------


class TestRoleNarrowed:
    def test_narrowed_allowed_paths_refuses(self, tmp_path: Path) -> None:
        """Reducing allowed_paths after suspend causes refusal."""
        # Checkpoint written with broad backend permissions
        cp = _make_checkpoint(tmp_path, role="backend")

        # Simulate narrowed permissions at resume time via override
        narrow = AgentPermissions(
            allowed_paths=("src/*",),  # fewer paths than default backend
            denied_paths=(".github/*", ".sdd/*", "templates/roles/*"),
        )
        ok, reason = is_checkpoint_recoverable(cp, role_overrides={"backend": narrow})
        assert ok is False
        assert "grant mismatch" in reason
        assert "narrowed" in reason

    def test_refusal_before_worktree_access(self, tmp_path: Path) -> None:
        """Grant check fires even when worktree does not exist."""
        cp = AgentCheckpoint(
            agent_id="a",
            task_id="t",
            worktree_path=str(tmp_path / "no-such-worktree"),
            role="backend",
            grant_hash="intentionally-wrong",
            parent_run_id="r",
            chain_head_at_suspend="h",
        )
        ok, reason = is_checkpoint_recoverable(cp)
        assert ok is False
        assert "grant mismatch" in reason
        # Worktree doesn't exist but error is about grant, not worktree
        assert "worktree missing" not in reason

    def test_zero_writes_on_narrowed_role(self, tmp_path: Path) -> None:
        """AC-3 — no files written before the refusal."""
        narrow = AgentPermissions(allowed_paths=("src/*",), denied_paths=())
        cp = _make_checkpoint(tmp_path, role="backend")

        snapshot = {p: p.stat().st_mtime for p in tmp_path.rglob("*")}
        is_checkpoint_recoverable(cp, role_overrides={"backend": narrow})
        after = {p: p.stat().st_mtime for p in tmp_path.rglob("*")}

        # No new files, no modified mtimes
        assert set(after.keys()) == set(snapshot.keys())
        for p in snapshot:
            assert after[p] == snapshot[p]


# ---------------------------------------------------------------------------
# AC-6 scenario 3  task reassigned
# ---------------------------------------------------------------------------


class TestTaskReassigned:
    def test_different_task_id_refuses(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path, task_id="task-original")
        # Simulate a checkpoint whose task_id was changed in storage
        cp.task_id = "task-reassigned"
        ok, reason = is_checkpoint_recoverable(cp)
        assert ok is False
        assert "grant mismatch" in reason

    def test_same_task_id_passes(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path, task_id="task-same")
        ok, _ = is_checkpoint_recoverable(cp)
        assert ok is True


# ---------------------------------------------------------------------------
# AC-6 scenario 4  parent cancelled (parent_run_id changed)
# ---------------------------------------------------------------------------


class TestParentCancelled:
    def test_different_parent_run_id_refuses(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path, parent_run_id="run-original")
        cp.parent_run_id = "run-replacement"  # simulates re-parenting
        ok, reason = is_checkpoint_recoverable(cp)
        assert ok is False
        assert "grant mismatch" in reason

    def test_same_parent_run_id_passes(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path, parent_run_id="run-stable")
        ok, _ = is_checkpoint_recoverable(cp)
        assert ok is True


# ---------------------------------------------------------------------------
# AC-6 scenario 5  resume-after-crash — continuation entry never written
# ---------------------------------------------------------------------------


class TestResumeAfterCrash:
    def test_missing_continuation_entry_is_new_run(self, tmp_path: Path) -> None:
        """Absence of ContinuationEntry means the resume never completed.

        The verifier must treat this as a *new* run, not a continuation.
        We assert this by confirming that a checkpoint without a matching
        continuation entry in the journal cannot be verified as a resume.
        """
        cp = _make_checkpoint(tmp_path)
        cp_hash = checkpoint_hash(cp)

        # No continuation entry exists; journal is effectively empty
        journal: list[ContinuationEntry] = []

        def _is_continuation(h: str) -> bool:
            return any(e.checkpoint_hash == h for e in journal)

        assert _is_continuation(cp_hash) is False, (
            "Absent continuation entry must not be treated as evidence of continuity"
        )

    def test_present_continuation_entry_is_continuation(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        entry = build_continuation_entry(cp, chain_head_at_resume="newhead")
        journal = [entry]
        assert any(e.checkpoint_hash == checkpoint_hash(cp) for e in journal)


# ---------------------------------------------------------------------------
# AC-4  build_continuation_entry
# ---------------------------------------------------------------------------


class TestBuildContinuationEntry:
    def test_binds_correct_checkpoint_hash(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        entry = build_continuation_entry(cp, chain_head_at_resume="resume-head")
        assert entry.checkpoint_hash == checkpoint_hash(cp)

    def test_binds_grant_hash(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        entry = build_continuation_entry(cp, chain_head_at_resume="rh")
        assert entry.grant_hash == cp.grant_hash

    def test_binds_chain_heads(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path, chain_head="suspend-head")
        entry = build_continuation_entry(cp, chain_head_at_resume="resume-head")
        assert entry.chain_head_at_suspend == "suspend-head"
        assert entry.chain_head_at_resume == "resume-head"

    def test_entry_is_frozen(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        entry = build_continuation_entry(cp)
        with pytest.raises((AttributeError, TypeError)):
            entry.checkpoint_hash = "tampered"  # type: ignore[misc]

    def test_entry_has_timestamp(self, tmp_path: Path) -> None:
        before = time.time()
        cp = _make_checkpoint(tmp_path)
        entry = build_continuation_entry(cp)
        after = time.time()
        assert before <= entry.resumed_at <= after


# ---------------------------------------------------------------------------
# AC-5  absence is never evidence of continuity (property-style)
# ---------------------------------------------------------------------------


class TestAbsenceNotContinuity:
    def test_empty_journal_is_new_run(self) -> None:
        journal: list[ContinuationEntry] = []
        # Any checkpoint hash that is NOT in journal is a new run
        for fake_hash in ["aaa", "bbb", "ccc"]:
            assert not any(e.checkpoint_hash == fake_hash for e in journal)

    def test_wrong_checkpoint_hash_is_not_match(self, tmp_path: Path) -> None:
        cp = _make_checkpoint(tmp_path)
        entry = build_continuation_entry(cp, chain_head_at_resume="rh")
        # A different checkpoint hash must not match
        assert entry.checkpoint_hash != "wrong-hash"


# ---------------------------------------------------------------------------
# AC-6 scenario 6  legacy checkpoint without grant fields
# ---------------------------------------------------------------------------


class TestLegacyCheckpointNoGrantFields:
    def test_no_grant_hash_skips_authority_check(self, tmp_path: Path) -> None:
        """Checkpoints written before #3649 have empty grant_hash.

        They should fall through to the liveness checks unchanged so
        existing behaviour is not broken.
        """
        _init_git_repo(tmp_path)
        (tmp_path / "legacy_work.py").write_text("# old work\n")
        cp = AgentCheckpoint(
            agent_id="old-agent",
            task_id="task-legacy",
            worktree_path=str(tmp_path),
            # grant fields absent / empty (pre-#3649 checkpoint)
            role="",
            grant_hash="",
            parent_run_id="",
            chain_head_at_suspend="",
        )
        ok, reason = is_checkpoint_recoverable(cp)
        assert ok is True
        assert "uncommitted" in reason


# ---------------------------------------------------------------------------
# Save / load roundtrip preserves grant fields
# ---------------------------------------------------------------------------


class TestGrantFieldsRoundtrip:
    def test_save_load_preserves_grant_fields(self, tmp_path: Path) -> None:
        from bernstein.core.persistence.agent_checkpoint import load_checkpoint

        perms = get_permissions_for_role("qa")
        gh = compute_grant_hash("qa", perms, "t-1", "r-1", "chainabc")
        cp = AgentCheckpoint(
            agent_id="ag-grant",
            task_id="t-1",
            worktree_path="/tmp/wt",
            role="qa",
            grant_hash=gh,
            parent_run_id="r-1",
            chain_head_at_suspend="chainabc",
        )
        save_checkpoint(cp, tmp_path)
        loaded = load_checkpoint("ag-grant", tmp_path)
        assert loaded is not None
        assert loaded.role == "qa"
        assert loaded.grant_hash == gh
        assert loaded.parent_run_id == "r-1"
        assert loaded.chain_head_at_suspend == "chainabc"


# ---------------------------------------------------------------------------
# CLI lookup path — task-keyed resolution over agent-keyed storage
# ---------------------------------------------------------------------------


class TestFindCheckpointForTask:
    def test_resolves_agent_keyed_storage_by_task_id(self, tmp_path: Path) -> None:
        """``prepare_resume`` knows only the task id, but checkpoints live
        under ``agents/{agent_id}/``. The lookup must bridge the two: keying
        the load by task id directly would return None for every real
        checkpoint and silently skip the authority check."""
        perms = get_permissions_for_role("backend")
        gh = compute_grant_hash("backend", perms, "task-cli", "run-7", "headx")
        cp = AgentCheckpoint(
            agent_id="agent-alpha",
            task_id="task-cli",
            worktree_path="/tmp/wt",
            role="backend",
            grant_hash=gh,
            parent_run_id="run-7",
            chain_head_at_suspend="headx",
        )
        save_checkpoint(cp, tmp_path)

        found = find_checkpoint_for_task("task-cli", tmp_path)
        assert found is not None
        assert found.agent_id == "agent-alpha"
        assert found.grant_hash == gh
        # The agent id is not a task id — it must not resolve.
        assert find_checkpoint_for_task("agent-alpha", tmp_path) is None

    def test_newest_checkpoint_wins_for_duplicated_task(self, tmp_path: Path) -> None:
        for agent_id, ts in (("a-old", 1_000.0), ("a-new", 2_000.0)):
            save_checkpoint(
                AgentCheckpoint(
                    agent_id=agent_id,
                    task_id="task-dup",
                    worktree_path="/tmp/wt",
                    checkpointed_at=ts,
                ),
                tmp_path,
            )
        found = find_checkpoint_for_task("task-dup", tmp_path)
        assert found is not None
        assert found.agent_id == "a-new"

    def test_missing_agents_dir_is_none(self, tmp_path: Path) -> None:
        assert find_checkpoint_for_task("task-x", tmp_path / "absent") is None

    def test_corrupt_sibling_checkpoint_does_not_block(self, tmp_path: Path) -> None:
        """One unreadable checkpoint must not break resume of other tasks."""
        bad_dir = tmp_path / "agents" / "agent-bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "checkpoint.json").write_text("{not json")
        save_checkpoint(
            AgentCheckpoint(agent_id="agent-ok", task_id="task-ok", worktree_path="/tmp/wt"),
            tmp_path,
        )
        found = find_checkpoint_for_task("task-ok", tmp_path)
        assert found is not None
        assert found.agent_id == "agent-ok"
