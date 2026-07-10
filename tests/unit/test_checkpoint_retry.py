"""Checkpointed retries: journal-anchored warm/fork/cold retry decisions (#2359).

A failed task historically restarted from zero even when the underlying agent
CLI could continue the native session warm. These tests pin the substrate:

* Per-adapter checkpoint-retry capability map derived from the declared
  resume strategy matrix (single source of truth, AC4).
* Checkpoint references (native session id + workspace hash) recorded as
  Merkle-chained rows in the task's existing event journal.
* The retry decision is a pure, deterministic projection of its inputs, and
  the recorded decision carries a stable ``decision_hash``.
* Workspace-hash mismatch downgrades to a cold restart, recorded as such
  (AC3); a tampered journal can never fuel a warm resume.
* The decision is recorded in the HMAC audit chain and anchored in the run
  lineage spine (AC2).

Each test fails against the pre-checkpoint-retry code and passes after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.adapters._contract import (
    CHECKPOINT_RETRY_CAPABILITY_MATRIX,
    STRATEGY_MATRIX,
    CheckpointRetryCapability,
    ResumeStrategy,
    checkpoint_retry_capability,
)
from bernstein.core.security.audit_chain import (
    EVENT_CHECKPOINT_RETRY,
    AuditChainStore,
)
from bernstein.core.tasks.checkpoint_retry import (
    CORRECTIVE_INSTRUCTION_TEMPLATES,
    CheckpointRef,
    RetryMode,
    build_retry_prompt,
    decide_retry,
    latest_checkpoint,
    record_retry_decision,
    record_task_checkpoint,
    render_corrective_instruction,
    stamp_checkpoint_retry_metadata,
    task_run_id,
    workspace_hash,
)

_KEY = b"0" * 32


def _make_worktree(root: Path) -> Path:
    tree = root / "wt"
    tree.mkdir(parents=True)
    (tree / "src").mkdir()
    (tree / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tree / "README.md").write_text("readme\n", encoding="utf-8")
    return tree


def _record(sdd: Path, tree: Path, task_id: str = "t1", adapter: str = "claude") -> CheckpointRef:
    return record_task_checkpoint(
        sdd_dir=sdd,
        task_id=task_id,
        adapter=adapter,
        session_id="sess-abc",
        workspace_hash=workspace_hash(tree),
        worktree_path=str(tree),
    )


# ---------------------------------------------------------------------------
# Per-adapter capability map (AC4 substrate)
# ---------------------------------------------------------------------------


class TestCapabilityMap:
    def test_native_resume_adapters_are_warm_capable(self) -> None:
        assert checkpoint_retry_capability("claude") is CheckpointRetryCapability.FORK
        assert checkpoint_retry_capability("claude_routine") is CheckpointRetryCapability.FORK
        assert checkpoint_retry_capability("openai_agents") is CheckpointRetryCapability.RESUME

    def test_unsupported_resume_maps_to_none(self) -> None:
        for name in ("aider", "qwen", "mock", "cursor", "gemini", "opencode"):
            assert checkpoint_retry_capability(name) is CheckpointRetryCapability.NONE

    def test_unknown_adapter_defaults_to_none(self) -> None:
        assert checkpoint_retry_capability("no-such-adapter") is CheckpointRetryCapability.NONE

    def test_matrix_covers_every_declared_adapter(self) -> None:
        assert set(CHECKPOINT_RETRY_CAPABILITY_MATRIX) == set(STRATEGY_MATRIX)

    def test_capability_derives_from_resume_axis(self) -> None:
        # Single source of truth: an adapter whose declared resume strategy is
        # UNSUPPORTED can never be warm/fork capable.
        for name, strategy in STRATEGY_MATRIX.items():
            capability = CHECKPOINT_RETRY_CAPABILITY_MATRIX[name]
            if strategy.resume is ResumeStrategy.UNSUPPORTED:
                assert capability is CheckpointRetryCapability.NONE, name
            else:
                assert capability is not CheckpointRetryCapability.NONE, name


# ---------------------------------------------------------------------------
# Workspace hash (the safety-valve contract)
# ---------------------------------------------------------------------------


class TestWorkspaceHash:
    def test_deterministic_across_directories(self, tmp_path: Path) -> None:
        a = _make_worktree(tmp_path / "one")
        (tmp_path / "two").mkdir()
        b = _make_worktree(tmp_path / "two")
        assert workspace_hash(a) == workspace_hash(b)

    def test_content_change_changes_hash(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        before = workspace_hash(tree)
        (tree / "src" / "a.py").write_text("print('b')\n", encoding="utf-8")
        assert workspace_hash(tree) != before

    def test_cwd_independent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tree = _make_worktree(tmp_path)
        first = workspace_hash(tree)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert workspace_hash(tree) == first

    def test_git_and_sdd_internals_excluded(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        before = workspace_hash(tree)
        (tree / ".git").mkdir()
        (tree / ".git" / "index").write_bytes(b"gitstate")
        (tree / ".sdd").mkdir()
        (tree / ".sdd" / "runtime.json").write_text("{}", encoding="utf-8")
        assert workspace_hash(tree) == before


# ---------------------------------------------------------------------------
# Checkpoint references live in the task's event journal
# ---------------------------------------------------------------------------


class TestCheckpointRecording:
    def test_record_returns_journal_anchored_ref(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        ref = _record(sdd, tree)
        assert ref.task_id == "t1"
        assert ref.adapter == "claude"
        assert ref.session_id == "sess-abc"
        assert ref.journal_index == 0
        assert len(ref.event_hash) == 64
        int(ref.event_hash, 16)  # hex

    def test_journal_chain_verifies_after_checkpoints(self, tmp_path: Path) -> None:
        from bernstein.core.replay.journal import verify_journal

        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        _record(sdd, tree)
        _record(sdd, tree)
        path = sdd / "runs" / task_run_id("t1") / "journal.jsonl"
        result = verify_journal(path)
        assert result.ok
        assert result.count == 2

    def test_latest_checkpoint_returns_most_recent(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        first = _record(sdd, tree)
        second = record_task_checkpoint(
            sdd_dir=sdd,
            task_id="t1",
            adapter="claude",
            session_id="sess-later",
            workspace_hash=workspace_hash(tree),
            worktree_path=str(tree),
        )
        loaded = latest_checkpoint(sdd, "t1")
        assert loaded is not None
        assert loaded.session_id == "sess-later"
        assert loaded.event_hash == second.event_hash
        assert loaded.journal_index == first.journal_index + 1

    def test_latest_checkpoint_none_without_journal(self, tmp_path: Path) -> None:
        assert latest_checkpoint(tmp_path / ".sdd", "t1") is None

    def test_tampered_journal_never_fuels_warm_resume(self, tmp_path: Path) -> None:
        # A mutated checkpoint row breaks the Merkle chain; the loader must
        # fail closed (no checkpoint -> cold) rather than trust the row.
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        _record(sdd, tree)
        path = sdd / "runs" / task_run_id("t1") / "journal.jsonl"
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["session_id"] = "attacker-session"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        assert latest_checkpoint(sdd, "t1") is None

    def test_checkpoint_recording_survives_process_reopen(self, tmp_path: Path) -> None:
        # Each record call opens the journal fresh (as a new process would);
        # the chain must extend, not restart from genesis.
        from bernstein.core.replay.journal import load_events

        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        _record(sdd, tree)
        _record(sdd, tree)
        _record(sdd, tree)
        path = sdd / "runs" / task_run_id("t1") / "journal.jsonl"
        events = load_events(path)
        assert [e["index"] for e in events] == [0, 1, 2]
        assert events[1]["prev_hash"] == events[0]["event_hash"]
        assert events[2]["prev_hash"] == events[1]["event_hash"]


# ---------------------------------------------------------------------------
# Corrective instructions are templates, never freeform
# ---------------------------------------------------------------------------


class TestCorrectiveTemplates:
    def test_render_known_template(self) -> None:
        text = render_corrective_instruction(
            "gate_failure",
            gate_name="pytest",
            gate_output="FAILED tests/test_x.py::test_y - assert 1 == 2",
        )
        assert "pytest" in text
        assert "assert 1 == 2" in text

    def test_unknown_template_rejected(self) -> None:
        with pytest.raises(ValueError, match="gate_failure"):
            render_corrective_instruction("freeform", gate_name="g", gate_output="o")

    def test_every_template_is_parameterised(self) -> None:
        for template_id in CORRECTIVE_INSTRUCTION_TEMPLATES:
            text = render_corrective_instruction(template_id, gate_name="GATE-N", gate_output="GATE-OUT")
            assert "GATE-N" in text or "GATE-OUT" in text


# ---------------------------------------------------------------------------
# The retry decision is a deterministic projection
# ---------------------------------------------------------------------------


def _ref(tree: Path, *, adapter: str = "claude", session_id: str = "sess-abc") -> CheckpointRef:
    return CheckpointRef(
        task_id="t1",
        adapter=adapter,
        session_id=session_id,
        workspace_hash=workspace_hash(tree),
        worktree_path=str(tree),
        journal_index=0,
        event_hash="a" * 64,
    )


class TestDecideRetry:
    def test_warm_happy_path(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree)
        decision = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
            gate_name="pytest",
            gate_output="1 failed",
        )
        assert decision.effective_mode is RetryMode.WARM
        assert decision.workspace_match is True
        assert decision.downgrade_reason == ""
        assert decision.checkpoint_session_id == "sess-abc"
        assert decision.checkpoint_event_hash == "a" * 64
        assert "pytest" in decision.corrective_instruction

    def test_decision_is_deterministic(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree)
        kwargs = {
            "task_id": "t1",
            "requested_mode": "warm",
            "checkpoint": ref,
            "actual_workspace_hash": workspace_hash(tree),
            "gate_name": "pytest",
            "gate_output": "1 failed",
        }
        one = decide_retry(**kwargs)
        two = decide_retry(**kwargs)
        assert one == two
        assert one.decision_hash == two.decision_hash
        assert len(one.decision_hash) == 64

    def test_decision_hash_changes_with_inputs(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree)
        base = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
            gate_name="pytest",
            gate_output="1 failed",
        )
        other = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
            gate_name="pytest",
            gate_output="2 failed",
        )
        assert base.decision_hash != other.decision_hash

    def test_workspace_mismatch_downgrades_to_cold(self, tmp_path: Path) -> None:
        # AC3: mismatch between the recorded and actual workspace hash means
        # provider-side session state cannot be trusted -> cold, recorded.
        tree = _make_worktree(tmp_path)
        ref = _ref(tree)
        (tree / "src" / "a.py").write_text("mutated\n", encoding="utf-8")
        decision = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
        )
        assert decision.effective_mode is RetryMode.COLD
        assert decision.workspace_match is False
        assert decision.downgrade_reason == "workspace_hash_mismatch"
        assert decision.corrective_instruction == ""

    def test_no_capability_falls_back_to_cold(self, tmp_path: Path) -> None:
        # AC4: adapters without the capability fall back to cold.
        tree = _make_worktree(tmp_path)
        ref = _ref(tree, adapter="aider")
        decision = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
        )
        assert decision.effective_mode is RetryMode.COLD
        assert decision.downgrade_reason == "adapter_capability_none"

    def test_no_checkpoint_falls_back_to_cold(self) -> None:
        decision = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=None,
            actual_workspace_hash="",
        )
        assert decision.effective_mode is RetryMode.COLD
        assert decision.downgrade_reason == "no_checkpoint"

    def test_requested_cold_stays_cold(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree)
        decision = decide_retry(
            task_id="t1",
            requested_mode="cold",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
        )
        assert decision.effective_mode is RetryMode.COLD
        assert decision.downgrade_reason == ""
        assert decision.corrective_instruction == ""

    def test_fork_downgrades_to_warm_for_resume_only_adapter(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree, adapter="openai_agents")
        decision = decide_retry(
            task_id="t1",
            requested_mode="fork",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
        )
        assert decision.effective_mode is RetryMode.WARM
        assert decision.downgrade_reason == "fork_downgraded_to_warm"

    def test_fork_supported_for_fork_capable_adapter(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree, adapter="claude")
        decision = decide_retry(
            task_id="t1",
            requested_mode="fork",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
        )
        assert decision.effective_mode is RetryMode.FORK


# ---------------------------------------------------------------------------
# Recording: journal + audit chain + lineage spine (AC2, AC3)
# ---------------------------------------------------------------------------


class TestRecordRetryDecision:
    def _decide(self, tmp_path: Path, *, mutate: bool = False) -> tuple[Path, object]:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        ref = _record(sdd, tree)
        if mutate:
            (tree / "src" / "a.py").write_text("mutated\n", encoding="utf-8")
        decision = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
            gate_name="pytest",
            gate_output="1 failed",
        )
        return sdd, decision

    def test_decision_recorded_in_chain_and_lineage(self, tmp_path: Path) -> None:
        sdd, decision = self._decide(tmp_path)
        chain = AuditChainStore(sdd / "audit", key=_KEY)
        record = record_retry_decision(
            sdd_dir=sdd,
            decision=decision,  # type: ignore[arg-type]
            audit_chain=chain,
            hmac_key=_KEY,
        )
        # Audit chain mirror (AC2): mode + checkpoint reference.
        rows = chain.query(event_type=EVENT_CHECKPOINT_RETRY)
        assert len(rows) == 1
        details = rows[0].details
        assert details["task_id"] == "t1"
        assert details["retry_mode"] == "warm"
        assert details["requested_mode"] == "warm"
        assert details["checkpoint_event_hash"]
        assert details["decision_hash"]
        assert details["workspace_match"] is True
        assert "prev_chain_digest" in details
        ok, errors = chain.verify()
        assert ok, errors
        # Lineage spine anchor (AC2): the decision bytes are sealed.
        assert record.spine_entry_hash
        spine_file = sdd / "lineage" / task_run_id("t1") / "spine.jsonl"
        assert spine_file.exists()
        # Event-journal anchor: the decision row extends the task journal.
        from bernstein.core.replay.journal import verify_journal

        result = verify_journal(sdd / "runs" / task_run_id("t1") / "journal.jsonl")
        assert result.ok
        assert result.count == 2  # checkpoint + decision

    def test_cold_downgrade_recorded_as_cold(self, tmp_path: Path) -> None:
        # AC3: the mismatch-triggered cold restart is recorded as such.
        sdd, decision = self._decide(tmp_path, mutate=True)
        chain = AuditChainStore(sdd / "audit", key=_KEY)
        record_retry_decision(
            sdd_dir=sdd,
            decision=decision,  # type: ignore[arg-type]
            audit_chain=chain,
            hmac_key=_KEY,
        )
        rows = chain.query(event_type=EVENT_CHECKPOINT_RETRY)
        assert rows[0].details["retry_mode"] == "cold"
        assert rows[0].details["downgrade_reason"] == "workspace_hash_mismatch"
        assert rows[0].details["workspace_match"] is False

    def test_chain_record_never_carries_prompt_content(self, tmp_path: Path) -> None:
        sdd, decision = self._decide(tmp_path)
        chain = AuditChainStore(sdd / "audit", key=_KEY)
        record_retry_decision(
            sdd_dir=sdd,
            decision=decision,  # type: ignore[arg-type]
            audit_chain=chain,
            hmac_key=_KEY,
        )
        blob = repr(chain.query(event_type=EVENT_CHECKPOINT_RETRY)[0].details)
        assert "1 failed" not in blob  # gate output never mirrored
        assert "corrective" not in blob  # instruction text never mirrored


# ---------------------------------------------------------------------------
# Retry-path metadata stamp (integration surface)
# ---------------------------------------------------------------------------


class TestStampMetadata:
    def test_warm_stamp_with_matching_workspace(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        _record(sdd, tree)
        metadata = stamp_checkpoint_retry_metadata(
            metadata={"budget_multiplier": 1.0},
            task_id="t1",
            workdir=tmp_path,
            requested_mode="warm",
            gate_name="pytest",
            gate_output="1 failed",
        )
        assert metadata["retry_mode"] == "warm"
        assert metadata["retry_checkpoint_session_id"] == "sess-abc"
        assert metadata["retry_checkpoint_event_hash"]
        assert metadata["retry_decision_hash"]
        assert "pytest" in metadata["retry_corrective_instruction"]
        assert metadata["budget_multiplier"] == 1.0  # pre-existing keys intact

    def test_workspace_mismatch_stamps_cold(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        _record(sdd, tree)
        (tree / "src" / "a.py").write_text("mutated\n", encoding="utf-8")
        metadata = stamp_checkpoint_retry_metadata(
            metadata={},
            task_id="t1",
            workdir=tmp_path,
            requested_mode="warm",
        )
        assert metadata["retry_mode"] == "cold"
        assert metadata["retry_downgrade_reason"] == "workspace_hash_mismatch"
        assert "retry_corrective_instruction" not in metadata
        assert "retry_checkpoint_session_id" not in metadata

    def test_no_checkpoint_stamps_cold_without_side_effects(self, tmp_path: Path) -> None:
        # AC4: no capability / no checkpoint -> cold, metadata otherwise
        # unchanged (purely additive keys).
        metadata = stamp_checkpoint_retry_metadata(
            metadata={"original_task_id": "t1"},
            task_id="t1",
            workdir=tmp_path,
            requested_mode="warm",
        )
        assert metadata["retry_mode"] == "cold"
        assert metadata["retry_downgrade_reason"] == "no_checkpoint"
        assert metadata["original_task_id"] == "t1"

    def test_force_cold_wins_over_capable_checkpoint(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        _record(sdd, tree)
        metadata = stamp_checkpoint_retry_metadata(
            metadata={},
            task_id="t1",
            workdir=tmp_path,
            requested_mode="warm",
            force_cold=True,
        )
        assert metadata["retry_mode"] == "cold"
        assert metadata["retry_downgrade_reason"] == "fresh_context_restart"


# ---------------------------------------------------------------------------
# build_retry_prompt: cold replays everything, warm sends the corrective only
# ---------------------------------------------------------------------------


class TestBuildRetryPrompt:
    def test_cold_returns_full_prompt(self) -> None:
        decision = decide_retry(
            task_id="t1",
            requested_mode="cold",
            checkpoint=None,
            actual_workspace_hash="",
        )
        assert build_retry_prompt(decision, cold_prompt="FULL PROMPT") == "FULL PROMPT"

    def test_warm_returns_corrective_instruction(self, tmp_path: Path) -> None:
        tree = _make_worktree(tmp_path)
        ref = _ref(tree)
        decision = decide_retry(
            task_id="t1",
            requested_mode="warm",
            checkpoint=ref,
            actual_workspace_hash=workspace_hash(tree),
            gate_name="pytest",
            gate_output="1 failed",
        )
        prompt = build_retry_prompt(decision, cold_prompt="FULL PROMPT")
        assert prompt == decision.corrective_instruction
        assert "FULL PROMPT" not in prompt
