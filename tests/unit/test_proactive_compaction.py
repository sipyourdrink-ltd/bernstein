"""Tests for proactive threshold-triggered compaction (issue #2246).

Covers:
- Settings resolution from None / mapping / object sources with
  validation fallbacks.
- AC #4: a worker at threshold is compacted through the pipeline
  without any context_overflow failure; the compacted description is
  patched onto the task and the receipt is chained, journaled, and
  ledgered.
- Threshold / feature-flag / max_per_task guards.
- AC #3: a summary that fails the validators is aborted and never
  reaches the worker (no PATCH); the fix pass can repair it once.
- The proactive meta-message does not consume the reactive retry
  budget (the reactive path stays an unchanged fallback).
- The token-monitor tick hook invokes the proactive lane.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.core.orchestration.proactive_compaction import (
    DEFAULT_MAX_PER_TASK,
    DEFAULT_THRESHOLD,
    PROACTIVE_META_MARKER,
    PROACTIVE_REASON,
    CompactionSettings,
    maybe_compact_proactively,
    resolve_compaction_settings,
)
from bernstein.core.persistence.journal import JournalReader, agent_journal_dir
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.tokens.compaction_receipt import (
    find_compaction_steps,
    load_receipts,
    reconcile_with_ledger,
    verify_compaction_receipts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLAIN_DESCRIPTION = "\n".join(f"Step {i}: narrative describing work item number {i}." for i in range(40))

# Contains a quoted error string the deterministic structural summary
# drops, so validators reject the summary (adversarial fixture, AC #3).
_UNSUMMARIZABLE_DESCRIPTION = _PLAIN_DESCRIPTION + '\nThe last run failed with "ValueError: bad frobnication" today.'


def _ok_response(payload: Any = None) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload if payload is not None else []
    return response


def _make_orch(
    tmp_path: Path,
    *,
    description: str = _PLAIN_DESCRIPTION,
    compaction: Any = None,
    fix_call: Any = None,
) -> SimpleNamespace:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        max_task_retries=3,
        compaction=compaction if compaction is not None else {"proactive": True},
    )
    orch._workdir = tmp_path
    orch._client = MagicMock()
    orch._client.get.return_value = _ok_response({"id": "T-9", "description": description, "meta_messages": []})
    orch._client.patch.return_value = _ok_response()
    orch._plugin_manager = None
    orch._spend_ledger = None
    if fix_call is not None:
        orch._compaction_fix_call = fix_call
    return orch


def _make_session(task_id: str = "T-9") -> SimpleNamespace:
    return SimpleNamespace(id="sess-9", task_ids=[task_id], context_utilization_pct=85.0)


@pytest.fixture(autouse=True)
def _audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


@pytest.fixture(autouse=True)
def _quiet_metrics(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    collector = MagicMock()
    monkeypatch.setattr(
        "bernstein.core.orchestration.proactive_compaction.get_collector",
        lambda: collector,
    )
    return collector


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


class TestResolveCompactionSettings:
    def test_none_is_disabled_with_documented_defaults(self) -> None:
        settings = resolve_compaction_settings(None)
        assert settings == CompactionSettings(proactive=False, threshold=0.8, max_per_task=1)
        assert settings.threshold == DEFAULT_THRESHOLD
        assert settings.max_per_task == DEFAULT_MAX_PER_TASK

    def test_mapping_source(self) -> None:
        settings = resolve_compaction_settings({"proactive": True, "threshold": 0.7, "max_per_task": 3})
        assert settings == CompactionSettings(proactive=True, threshold=0.7, max_per_task=3)

    def test_object_source(self) -> None:
        source = SimpleNamespace(proactive=True, threshold=0.9, max_per_task=2)
        assert resolve_compaction_settings(source) == CompactionSettings(True, 0.9, 2)

    def test_out_of_range_threshold_falls_back(self) -> None:
        assert resolve_compaction_settings({"proactive": True, "threshold": 1.7}).threshold == DEFAULT_THRESHOLD
        assert resolve_compaction_settings({"proactive": True, "threshold": 0.0}).threshold == DEFAULT_THRESHOLD

    def test_bad_max_per_task_falls_back(self) -> None:
        assert resolve_compaction_settings({"max_per_task": 0}).max_per_task == DEFAULT_MAX_PER_TASK
        assert resolve_compaction_settings({"max_per_task": "many"}).max_per_task == DEFAULT_MAX_PER_TASK


# ---------------------------------------------------------------------------
# AC #4: threshold compaction without context_overflow
# ---------------------------------------------------------------------------


class TestProactiveHappyPath:
    def test_worker_at_threshold_is_compacted_and_receipted(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path)
        session = _make_session()

        compacted = maybe_compact_proactively(orch, session, utilization_fraction=0.85)
        assert compacted is True

        # The compacted description reached the task server via PATCH.
        patch_call = orch._client.patch.call_args
        assert patch_call is not None
        assert patch_call.args[0] == "http://server/tasks/T-9"
        body = patch_call.kwargs["json"]
        assert body["description"] != _PLAIN_DESCRIPTION
        assert len(body["description"]) < len(_PLAIN_DESCRIPTION)
        # The meta marker must NOT contain the reactive retry marker
        # ("CONTEXT COMPACTION"), or it would consume the 413 retry budget.
        assert any(PROACTIVE_META_MARKER in m for m in body["meta_messages"])
        assert not any("CONTEXT COMPACTION" in m for m in body["meta_messages"])

        # Receipt is chained and verifiable, journal step registered.
        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        receipts = load_receipts(chain, task_id="T-9")
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.trigger == "proactive"
        assert receipt.worker_id == "sess-9"
        assert receipt.tokens_before > receipt.tokens_after

        reader = JournalReader(agent_journal_dir(tmp_path / ".sdd", "sess-9"))
        steps = find_compaction_steps(reader)
        assert len(steps) == 1
        ok, errors = verify_compaction_receipts(chain, journal_reader=reader)
        assert ok, errors

    def test_metrics_extended_with_trigger(self, tmp_path: Path, _quiet_metrics: MagicMock) -> None:
        orch = _make_orch(tmp_path)
        maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.9)
        record = _quiet_metrics.record_compaction
        assert record.call_count == 1
        assert record.call_args.kwargs["reason"] == PROACTIVE_REASON
        assert record.call_args.kwargs["trigger"] == "proactive"

    def test_ledger_delta_reconciles_with_receipt(self, tmp_path: Path) -> None:
        from bernstein.core.cost.spend_ledger import SpendLedger

        orch = _make_orch(tmp_path)
        orch._spend_ledger = SpendLedger(path=tmp_path / "ledger.jsonl", run_id="r1")

        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.85)

        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        receipts = load_receipts(chain, task_id="T-9")
        entries = SpendLedger.load_entries(tmp_path / "ledger.jsonl")
        ok, errors = reconcile_with_ledger(receipts, entries)
        assert ok, errors


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestProactiveGuards:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, compaction={})
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=1.0) is False
        orch._client.patch.assert_not_called()

    def test_below_threshold_is_a_no_op(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path)
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.5) is False
        orch._client.get.assert_not_called()
        orch._client.patch.assert_not_called()

    def test_max_per_task_bounds_attempts(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, compaction={"proactive": True, "max_per_task": 1})
        session = _make_session()
        assert maybe_compact_proactively(orch, session, utilization_fraction=0.9) is True
        assert maybe_compact_proactively(orch, session, utilization_fraction=0.95) is False
        assert orch._client.patch.call_count == 1

    def test_session_without_task_is_skipped(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path)
        session = SimpleNamespace(id="sess-9", task_ids=[])
        assert maybe_compact_proactively(orch, session, utilization_fraction=0.9) is False

    def test_custom_threshold_respected(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, compaction={"proactive": True, "threshold": 0.95})
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.9) is False
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.96) is True


# ---------------------------------------------------------------------------
# AC #3: invalid summaries never reach the worker
# ---------------------------------------------------------------------------


class TestValidatorAbort:
    def test_invalid_summary_aborts_and_never_patches(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, description=_UNSUMMARIZABLE_DESCRIPTION)
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.9) is False
        orch._client.patch.assert_not_called()
        # No receipt is chained for an aborted compaction: nothing mutated.
        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        assert load_receipts(chain) == []

    def test_fix_pass_repairs_summary_once(self, tmp_path: Path) -> None:
        fixed_summary = 'compact summary; kept "ValueError: bad frobnication" verbatim.'
        calls: list[str] = []

        def fix_call(prompt: str) -> str:
            calls.append(prompt)
            return fixed_summary

        orch = _make_orch(tmp_path, description=_UNSUMMARIZABLE_DESCRIPTION, fix_call=fix_call)
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.9) is True
        assert len(calls) == 1
        assert "Do NOT re-summarize" in calls[0]

        body = orch._client.patch.call_args.kwargs["json"]
        assert body["description"] == fixed_summary

        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        [receipt] = load_receipts(chain, task_id="T-9")
        assert receipt.retry_count == 1

    def test_failed_fix_pass_aborts(self, tmp_path: Path) -> None:
        def fix_call(prompt: str) -> str:
            return "still summarised away the error"

        orch = _make_orch(tmp_path, description=_UNSUMMARIZABLE_DESCRIPTION, fix_call=fix_call)
        assert maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.9) is False
        orch._client.patch.assert_not_called()


# ---------------------------------------------------------------------------
# Reactive fallback stays unchanged
# ---------------------------------------------------------------------------


class TestReactiveBudgetUntouched:
    def test_proactive_meta_does_not_count_as_reactive_retry(self) -> None:
        from bernstein.core.orchestration.proactive_compaction import build_proactive_meta

        meta = build_proactive_meta(utilization_fraction=0.85)
        # agent_lifecycle counts meta messages containing this marker
        # toward the reactive compaction retry cap.
        assert "CONTEXT COMPACTION" not in meta
        assert PROACTIVE_META_MARKER in meta


# ---------------------------------------------------------------------------
# Token-monitor tick wiring
# ---------------------------------------------------------------------------


class TestTickWiring:
    def test_tick_hook_converts_pct_to_fraction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core.tokens import token_monitor

        seen: dict[str, Any] = {}

        def fake_maybe(orch: Any, session: Any, *, utilization_fraction: float) -> bool:
            seen["fraction"] = utilization_fraction
            return True

        monkeypatch.setattr(
            "bernstein.core.orchestration.proactive_compaction.maybe_compact_proactively",
            fake_maybe,
        )
        session = SimpleNamespace(id="s1", task_ids=["T-1"], context_utilization_pct=84.0)
        result = token_monitor._handle_proactive_compaction(SimpleNamespace(), session)
        assert result is True
        assert seen["fraction"] == pytest.approx(0.84)

    def test_tick_hook_skips_zero_utilization(self) -> None:
        from bernstein.core.tokens import token_monitor

        session = SimpleNamespace(id="s1", task_ids=["T-1"], context_utilization_pct=0.0)
        assert token_monitor._handle_proactive_compaction(SimpleNamespace(), session) is False


# ---------------------------------------------------------------------------
# Reactive path emits receipts too
# ---------------------------------------------------------------------------


class TestReactiveReceipt:
    def test_reactive_compaction_records_receipt(self, tmp_path: Path) -> None:
        from bernstein.core.agent_lifecycle import _try_compact_and_retry
        from bernstein.core.models import (
            AgentSession,
            Complexity,
            ModelConfig,
            Scope,
            Task,
            TaskStatus,
            TaskType,
        )

        (tmp_path / ".sdd" / "audit").mkdir(parents=True)
        task = Task(
            id="T-413",
            title="Implement feature",
            description="Write the code for the new feature module.\n" * 20,
            role="backend",
            status=TaskStatus.OPEN,
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            task_type=TaskType.STANDARD,
            meta_messages=[],
        )
        session = AgentSession(
            id="sess-413",
            role="backend",
            provider="claude",
            model_config=ModelConfig("sonnet", "high"),
            task_ids=["T-413"],
        )
        orch = SimpleNamespace()
        orch._config = SimpleNamespace(
            server_url="http://server",
            max_task_retries=3,
            recovery="restart",
            max_crash_retries=3,
        )
        orch._client = MagicMock()
        orch._client.patch.return_value = _ok_response()
        orch._client.post.return_value = _ok_response()
        orch._client.get.return_value = _ok_response()
        orch._workdir = tmp_path
        orch._retried_task_ids = set()
        orch._record_provider_health = MagicMock()
        orch._evolution = None
        orch._wal_writer = None
        orch._crash_counts = {}
        orch._spawner = MagicMock()
        orch._plugin_manager = None

        snapshot = {"open": [task], "claimed": [], "in_progress": [], "done": []}
        assert _try_compact_and_retry(
            orch=orch,
            task=task,
            task_id="T-413",
            session=session,
            tasks_snapshot=snapshot,
            fallback_model=None,
        )

        chain = AuditChainStore(tmp_path / ".sdd" / "audit")
        receipts = load_receipts(chain, task_id="T-413")
        assert len(receipts) == 1
        assert receipts[0].trigger == "reactive"
        assert receipts[0].worker_id == "sess-413"

        reader = JournalReader(agent_journal_dir(tmp_path / ".sdd", "sess-413"))
        ok, errors = verify_compaction_receipts(chain, journal_reader=reader)
        assert ok, errors
        assert len(find_compaction_steps(reader)) == 1


def test_journal_row_is_json(tmp_path: Path) -> None:
    """Sanity: journal rows written by the proactive path parse as JSON."""
    orch = _make_orch(tmp_path)
    maybe_compact_proactively(orch, _make_session(), utilization_fraction=0.85)
    bucket = agent_journal_dir(tmp_path / ".sdd", "sess-9") / "000000.jsonl"
    for line in bucket.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)
