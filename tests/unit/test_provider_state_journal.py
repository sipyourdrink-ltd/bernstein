"""Tests for provider-side context mutation journal entries (issue #2507).

Deterministic replay assumes context-as-sent equals context-as-consumed.
Provider-side context rewrites (compaction and similar opaque state) break
that assumption invisibly unless every observed mutation is chained into
the run journal before anything builds on it. These tests pin the issue's
acceptance criteria:

* AC1: a recorded mutation is load-bearing in the chain, not a side log.
* AC2: replay diff attributes a missing mutation entry with the
  ``provider_state_mutation`` reason code, the mutation kind, and the
  exact step index.
* AC3: in deterministic mode an arriving mutation is recorded flagged and
  verification fails closed.
* AC4: an adapter that cannot observe mutations produces a declared-blind
  capability record.
* AC5: journals without mutation entries verify unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bernstein.core.replay.diff import (
    REASON_CODE_PROVIDER_STATE_MUTATION,
    REASON_CODE_RESPONSE_MISMATCH,
    diff_event_logs,
)
from bernstein.core.replay.journal import EventJournal, verify_journal
from bernstein.core.replay.provider_state import (
    CAPABILITY_DECLARED_BLIND,
    CAPABILITY_OBSERVED,
    MUTATION_CAPABILITY_EVENT,
    PROVIDER_STATE_MUTATION_EVENT,
    ProviderStateMutation,
    deterministic_mode_active,
    mutation_from_signal,
    record_agent_mutations,
    record_mutation_capability,
    record_provider_state_mutation,
    verify_provider_state,
)


def _journal_with_mutation(sdd_dir: Path, run_id: str, *, flagged: bool = False) -> EventJournal:
    """Build a journal whose third entry is a provider-side mutation."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    # Payloads are identical across fixtures so byte-identical executions
    # chain to the same head regardless of the journal's directory name.
    journal.record("run_started", plan="plan.yaml")
    journal.record("task_claimed", task_id="T-1")
    mutation = ProviderStateMutation(
        kind="compact_boundary",
        before_digest="a" * 64,
        after_digest="b" * 64,
        step_index=0,
        flagged=flagged,
    )
    record_provider_state_mutation(journal, mutation)
    journal.record("task_completed", task_id="T-1")
    return journal


class TestContentAddressing:
    def test_content_address_is_stable_over_identity_fields(self) -> None:
        m1 = ProviderStateMutation(kind="compact_boundary", before_digest="a", after_digest="b", step_index=3)
        m2 = ProviderStateMutation(kind="compact_boundary", before_digest="a", after_digest="b", step_index=3)
        assert m1.content_address() == m2.content_address()

    def test_content_address_changes_with_any_identity_field(self) -> None:
        base = ProviderStateMutation(kind="compact_boundary", before_digest="a", after_digest="b", step_index=3)
        variants = [
            ProviderStateMutation(kind="context_edit", before_digest="a", after_digest="b", step_index=3),
            ProviderStateMutation(kind="compact_boundary", before_digest="x", after_digest="b", step_index=3),
            ProviderStateMutation(kind="compact_boundary", before_digest="a", after_digest="y", step_index=3),
            ProviderStateMutation(kind="compact_boundary", before_digest="a", after_digest="b", step_index=4),
        ]
        for variant in variants:
            assert variant.content_address() != base.content_address()

    def test_flag_does_not_change_content_address(self) -> None:
        """The flag is policy state, not mutation identity."""
        plain = ProviderStateMutation(kind="k", before_digest="a", after_digest="b", step_index=0)
        flagged = ProviderStateMutation(kind="k", before_digest="a", after_digest="b", step_index=0, flagged=True)
        assert plain.content_address() == flagged.content_address()

    def test_mutation_from_signal_digests_reported_metadata(self) -> None:
        detail = {"trigger": "auto", "pre_tokens": 90000, "post_tokens": 20000}
        m = mutation_from_signal("compact_boundary", detail, step_index=2)
        n = mutation_from_signal("compact_boundary", dict(detail), step_index=2)
        assert m == n
        assert m.before_digest
        assert m.after_digest
        assert m.before_digest != m.after_digest


class TestChainLoadBearing:
    """AC1: the mutation record is load-bearing in the chain."""

    def test_two_recordings_with_mutation_produce_identical_heads(self, tmp_path: Path) -> None:
        a = _journal_with_mutation(tmp_path / "a", "run-a")
        b = _journal_with_mutation(tmp_path / "b", "run-b")
        assert a.head() == b.head()
        assert verify_journal(a.path).ok
        assert verify_journal(b.path).ok

    def test_editing_the_mutation_entry_breaks_chain_at_its_index(self, tmp_path: Path) -> None:
        journal = _journal_with_mutation(tmp_path, "run-edit")
        rows = journal.path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(rows[2])
        assert tampered["event"] == PROVIDER_STATE_MUTATION_EVENT
        tampered["after_digest"] = "c" * 64
        rows[2] = json.dumps(tampered)
        journal.path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        result = verify_journal(journal.path)
        assert not result.ok
        assert result.divergent_index == 2

    def test_removing_the_mutation_entry_breaks_chain_at_its_index(self, tmp_path: Path) -> None:
        journal = _journal_with_mutation(tmp_path, "run-drop")
        rows = journal.path.read_text(encoding="utf-8").splitlines()
        del rows[2]
        journal.path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        result = verify_journal(journal.path)
        assert not result.ok
        assert result.divergent_index == 2

    def test_journal_without_mutations_verifies_unchanged(self, tmp_path: Path) -> None:
        """AC5: pre-existing journals are unaffected by the new event type."""
        journal = EventJournal(run_id="run-legacy", sdd_dir=tmp_path)
        journal.record("run_started", run_id="run-legacy")
        journal.record("task_completed", task_id="T-1")

        assert verify_journal(journal.path).ok
        state = verify_provider_state(journal.path)
        assert state.ok
        assert state.mutation_count == 0
        assert state.flagged_indices == []


class TestDeterministicPolicy:
    """AC3: deterministic mode fails closed on arriving mutations."""

    def test_deterministic_mode_reads_seed_and_replay_envs(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert not deterministic_mode_active()
        with patch.dict("os.environ", {"BERNSTEIN_DETERMINISTIC_SEED": "42"}, clear=True):
            assert deterministic_mode_active()
        with patch.dict("os.environ", {"BERNSTEIN_REPLAY_RUN_ID": "run-1"}, clear=True):
            assert deterministic_mode_active()

    def test_flagged_mutation_fails_verification_closed(self, tmp_path: Path) -> None:
        journal = _journal_with_mutation(tmp_path, "run-flagged", flagged=True)

        assert verify_journal(journal.path).ok  # the chain itself is intact
        state = verify_provider_state(journal.path)
        assert not state.ok
        assert state.flagged_indices == [2]
        assert any("compact_boundary" in err for err in state.errors)

    def test_unflagged_mutation_is_pinned_and_accepted(self, tmp_path: Path) -> None:
        journal = _journal_with_mutation(tmp_path, "run-pinned", flagged=False)
        state = verify_provider_state(journal.path)
        assert state.ok
        assert state.mutation_count == 1

    def test_tampered_content_address_fails_verification(self, tmp_path: Path) -> None:
        journal = _journal_with_mutation(tmp_path, "run-addr")
        rows = journal.path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(rows[2])
        tampered["content_address"] = "0" * 64
        rows[2] = json.dumps(tampered)
        journal.path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        state = verify_provider_state(journal.path)
        assert not state.ok

    def test_record_agent_mutations_flags_in_deterministic_mode(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="run-det", sdd_dir=tmp_path)
        signals = [{"kind": "compact_boundary", "detail": {"trigger": "auto", "pre_tokens": 1}}]
        with patch.dict("os.environ", {"BERNSTEIN_DETERMINISTIC_SEED": "7"}, clear=False):
            recorded = record_agent_mutations(journal, signals, agent_id="agent-1")

        assert recorded == 1
        rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["event"] == PROVIDER_STATE_MUTATION_EVENT
        assert rows[0]["flagged"] is True
        assert rows[0]["agent_id"] == "agent-1"
        assert not verify_provider_state(journal.path).ok

    def test_record_agent_mutations_pins_outside_deterministic_mode(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="run-live", sdd_dir=tmp_path)
        signals = [
            {"kind": "compact_boundary", "detail": {"trigger": "auto"}},
            {"kind": "context_edit", "detail": {"edited": True}},
        ]
        with patch.dict("os.environ", {}, clear=True):
            recorded = record_agent_mutations(journal, signals, agent_id="agent-2")

        assert recorded == 2
        rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        assert [r["flagged"] for r in rows] == [False, False]
        assert [r["step_index"] for r in rows] == [0, 1]
        assert verify_provider_state(journal.path).ok


class TestDivergenceAttribution:
    """AC2: replay diff names the mutation, its kind, and the step index."""

    def _write_log(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_missing_mutation_entry_attributed_with_reason_code(self, tmp_path: Path) -> None:
        common = [
            {"event": "run_started", "payload_hash": "p0"},
            {"event": "task_claimed", "payload_hash": "p1"},
        ]
        mutation_row = {
            "event": PROVIDER_STATE_MUTATION_EVENT,
            "mutation_kind": "compact_boundary",
            "payload_hash": "p2",
        }
        tail = [{"event": "task_completed", "payload_hash": "p3"}]

        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        self._write_log(path_a, [*common, mutation_row, *tail])
        self._write_log(path_b, [*common, *tail])

        result = diff_event_logs(path_a, path_b)
        assert result.diverged
        assert result.index == 2
        assert result.reason_code == REASON_CODE_PROVIDER_STATE_MUTATION
        assert result.mutation_kind == "compact_boundary"
        assert "compact_boundary" in result.reason
        assert "2" in result.reason

    def test_trailing_mutation_entry_attributed(self, tmp_path: Path) -> None:
        common = [{"event": "run_started", "payload_hash": "p0"}]
        mutation_row = {
            "event": PROVIDER_STATE_MUTATION_EVENT,
            "mutation_kind": "context_edit",
            "payload_hash": "p1",
        }
        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        self._write_log(path_a, [*common, mutation_row])
        self._write_log(path_b, common)

        result = diff_event_logs(path_a, path_b)
        assert result.diverged
        assert result.index == 1
        assert result.reason_code == REASON_CODE_PROVIDER_STATE_MUTATION
        assert result.mutation_kind == "context_edit"

    def test_plain_response_mismatch_keeps_generic_reason(self, tmp_path: Path) -> None:
        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        self._write_log(path_a, [{"kind": "llm", "key": "k", "response": "one"}])
        self._write_log(path_b, [{"kind": "llm", "key": "k", "response": "two"}])

        result = diff_event_logs(path_a, path_b)
        assert result.diverged
        assert result.reason_code == REASON_CODE_RESPONSE_MISMATCH
        assert result.mutation_kind == ""

    def test_gateway_style_mutation_row_also_attributed(self, tmp_path: Path) -> None:
        path_a = tmp_path / "a.jsonl"
        path_b = tmp_path / "b.jsonl"
        self._write_log(
            path_a,
            [
                {"kind": "llm", "key": "k", "response": "r"},
                {"kind": PROVIDER_STATE_MUTATION_EVENT, "mutation_kind": "compact_boundary"},
            ],
        )
        self._write_log(path_b, [{"kind": "llm", "key": "k", "response": "r"}])

        result = diff_event_logs(path_a, path_b)
        assert result.diverged
        assert result.index == 1
        assert result.reason_code == REASON_CODE_PROVIDER_STATE_MUTATION


class TestCapabilityRecord:
    """AC4: observability capability is recorded per run."""

    def test_record_mutation_capability_writes_journal_row(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="run-cap", sdd_dir=tmp_path)
        record_mutation_capability(journal, adapter="claude", capability=CAPABILITY_OBSERVED)

        rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["event"] == MUTATION_CAPABILITY_EVENT
        assert rows[0]["adapter"] == "claude"
        assert rows[0]["capability"] == CAPABILITY_OBSERVED
        assert verify_journal(journal.path).ok

    def test_declared_blind_record_is_distinguishable(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="run-blind", sdd_dir=tmp_path)
        record_mutation_capability(journal, adapter="generic", capability=CAPABILITY_DECLARED_BLIND)

        rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["capability"] == CAPABILITY_DECLARED_BLIND

    def test_capability_for_provider_resolves_claude_as_observed(self) -> None:
        from bernstein.core.replay.provider_state import capability_for_provider

        name, capability = capability_for_provider("claude", "sonnet")
        assert name == "claude"
        assert capability == CAPABILITY_OBSERVED

    def test_capability_for_unresolvable_provider_is_declared_blind(self) -> None:
        from bernstein.core.replay.provider_state import capability_for_provider

        name, capability = capability_for_provider("no-such-provider-2507", "")
        assert capability == CAPABILITY_DECLARED_BLIND
        assert name == "no-such-provider-2507"


class TestVerifyCliFailClosed:
    """AC3 surface: ``bernstein replay --verify`` exits non-zero on a flag."""

    def test_verify_exits_nonzero_on_flagged_mutation(self, tmp_path: Path) -> None:
        from bernstein.cli.advanced_cmd import replay_cmd
        from click.testing import CliRunner

        sdd_dir = tmp_path / ".sdd"
        _journal_with_mutation(sdd_dir, "run-cli-flag", flagged=True)

        result = CliRunner().invoke(replay_cmd, ["run-cli-flag", "--sdd-dir", str(sdd_dir), "--verify"])
        assert result.exit_code == 1
        assert "provider" in result.output.lower()

    def test_verify_accepts_pinned_mutation(self, tmp_path: Path) -> None:
        from bernstein.cli.advanced_cmd import replay_cmd
        from click.testing import CliRunner

        sdd_dir = tmp_path / ".sdd"
        _journal_with_mutation(sdd_dir, "run-cli-pin", flagged=False)

        result = CliRunner().invoke(replay_cmd, ["run-cli-pin", "--sdd-dir", str(sdd_dir), "--verify"])
        assert result.exit_code == 0


class TestOrchestratorWiring:
    """The tick path records capability at spawn and chains mutations at reap.

    Binds the real orchestrator methods onto a minimal stub (the pattern
    used in ``tests/unit/orchestration/test_orchestrator_tick_methods.py``)
    so the genuine implementation runs against a real journal.
    """

    @staticmethod
    def _stub(tmp_path: Path) -> object:
        from types import MethodType, SimpleNamespace

        from bernstein.core.orchestration.orchestrator import Orchestrator

        stub = SimpleNamespace(
            _recorder=EventJournal(run_id="run-wire", sdd_dir=tmp_path / ".sdd"),
            _mutation_capability_recorded=set(),
            _agents={},
            _workdir=tmp_path,
        )
        stub._record_mutation_capability_once = MethodType(
            Orchestrator._record_mutation_capability_once,  # type: ignore[arg-type]
            stub,
        )
        stub._record_provider_mutations_for = MethodType(
            Orchestrator._record_provider_mutations_for,  # type: ignore[arg-type]
            stub,
        )
        return stub

    @staticmethod
    def _session(session_id: str, provider: str) -> object:
        from bernstein.core.tasks.models import AgentSession

        return AgentSession(id=session_id, role="worker", provider=provider)

    def _rows(self, stub: object) -> list[dict[str, object]]:
        path = stub._recorder.path  # type: ignore[attr-defined]
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_capability_recorded_once_per_provider_at_spawn(self, tmp_path: Path) -> None:
        stub = self._stub(tmp_path)
        session = self._session("agent-1", "claude")

        stub._record_mutation_capability_once(session)
        stub._record_mutation_capability_once(session)  # dedup within the run

        rows = [r for r in self._rows(stub) if r["event"] == MUTATION_CAPABILITY_EVENT]
        assert len(rows) == 1
        assert rows[0]["adapter"] == "claude"
        assert rows[0]["capability"] == CAPABILITY_OBSERVED

    def test_unresolvable_provider_recorded_declared_blind(self, tmp_path: Path) -> None:
        """AC4: inability to observe is recorded, never inferred from silence."""
        stub = self._stub(tmp_path)
        stub._record_mutation_capability_once(self._session("agent-2", "no-such-provider-2507"))

        rows = [r for r in self._rows(stub) if r["event"] == MUTATION_CAPABILITY_EVENT]
        assert len(rows) == 1
        assert rows[0]["capability"] == CAPABILITY_DECLARED_BLIND

    def test_reaped_agent_sidecar_signals_are_chained(self, tmp_path: Path) -> None:
        """Sidecar -> adapter -> journal, end to end through the real reap path."""
        stub = self._stub(tmp_path)
        stub._agents["agent-3"] = self._session("agent-3", "claude")  # type: ignore[attr-defined]
        sidecar_dir = tmp_path / ".sdd" / "runtime" / "provider_state"
        sidecar_dir.mkdir(parents=True)
        (sidecar_dir / "agent-3.jsonl").write_text(
            json.dumps({"kind": "compact_boundary", "detail": {"trigger": "auto"}}) + "\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {}, clear=True):
            stub._record_provider_mutations_for("agent-3")

        rows = [r for r in self._rows(stub) if r["event"] == PROVIDER_STATE_MUTATION_EVENT]
        assert len(rows) == 1
        assert rows[0]["mutation_kind"] == "compact_boundary"
        assert rows[0]["agent_id"] == "agent-3"
        assert rows[0]["flagged"] is False
        assert verify_provider_state(stub._recorder.path).ok  # type: ignore[attr-defined]

    def test_reaped_agent_without_sidecar_records_nothing(self, tmp_path: Path) -> None:
        stub = self._stub(tmp_path)
        stub._agents["agent-4"] = self._session("agent-4", "claude")  # type: ignore[attr-defined]

        stub._record_provider_mutations_for("agent-4")

        assert [r for r in self._rows(stub) if r["event"] == PROVIDER_STATE_MUTATION_EVENT] == []


class TestAuditMirror:
    def test_recorded_mutation_is_mirrored_into_audit_chain(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import (
            EVENT_PROVIDER_STATE_MUTATION,
            AuditChainStore,
        )

        journal = EventJournal(run_id="run-audit", sdd_dir=tmp_path / "sdd")
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        signals = [{"kind": "compact_boundary", "detail": {"trigger": "auto"}}]

        recorded = record_agent_mutations(
            journal,
            signals,
            agent_id="agent-3",
            deterministic=False,
            audit_chain=chain,
        )

        assert recorded == 1
        chain_text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "audit").glob("*.jsonl"))
        assert EVENT_PROVIDER_STATE_MUTATION in chain_text
        assert journal.head() in chain_text
