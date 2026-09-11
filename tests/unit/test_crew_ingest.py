"""Tests for role-and-crew ingest adapter (#4965)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters.crew_ingest import (
    CrewCallbackHandler,
    CrewHandoff,
    CrewIngestAdapter,
    CrewIngestPlugin,
    CrewRole,
    CrewTask,
    IngestedCrewRun,
)
from bernstein.core.identity.delegation import DelegationLedger
from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "govern" / "crew_run.json"
TEST_KEY = b"test-audit-key-32-bytes-long----"


@pytest.fixture
def ledger(tmp_path: Path) -> DelegationLedger:
    return DelegationLedger(root=tmp_path, key=TEST_KEY)


@pytest.fixture
def adapter() -> CrewIngestAdapter:
    return CrewIngestAdapter()


def test_recorded_fixture_run_ingests_with_every_role_and_task_present(adapter: CrewIngestAdapter) -> None:
    run = adapter.ingest_trace(FIXTURE_PATH)
    assert run.run_id == "crew-run-alpha-4965"
    assert run.crew_id == "research-analysis-crew"

    # Verify all roles present and sorted deterministically
    role_ids = [r.role_id for r in run.roles]
    assert role_ids == ["analyst", "coordinator", "researcher"]

    # Verify all tasks present and sorted deterministically
    task_ids = [t.task_id for t in run.tasks]
    assert task_ids == ["task-init", "task-search", "task-summarize"]

    # Verify tool calls inside tasks
    search_task = next(t for t in run.tasks if t.task_id == "task-search")
    assert len(search_task.tool_calls) == 1
    assert search_task.tool_calls[0].tool_name == "web_search"
    assert search_task.tool_calls[0].args_digest.startswith("sha256:")

    # Canonical bytes are stable
    canonical = run.canonical_bytes()
    assert isinstance(canonical, bytes)
    assert len(canonical) > 0


def test_each_handoff_produces_delegation_receipt_that_verifies(
    adapter: CrewIngestAdapter,
    ledger: DelegationLedger,
) -> None:
    run = adapter.ingest_trace(FIXTURE_PATH)
    receipts = adapter.record_handoffs_to_ledger(ledger, run)

    assert len(receipts) == 2
    assert receipts[0].issuer == "role:coordinator"
    assert receipts[0].subject == "role:researcher"
    assert receipts[0].act == "task.handoff"
    assert receipts[0].hop_index == 0

    assert receipts[1].issuer == "role:researcher"
    assert receipts[1].subject == "role:analyst"
    assert receipts[1].act == "task.handoff"
    assert receipts[1].hop_index == 1
    assert receipts[1].prev_hmac == receipts[0].hmac
    assert receipts[1].parent_ref == receipts[0].hmac

    # Offline verification passes
    result = adapter.verify_run(ledger, run.run_id, key=TEST_KEY)
    assert result.valid is True
    assert result.chain_ok is True
    assert result.hops == 2
    assert len(result.errors) == 0


def test_handoff_missing_parent_receipt_fails_closed(
    adapter: CrewIngestAdapter,
    ledger: DelegationLedger,
) -> None:
    # 1. Invalid parent handoff index
    invalid_index_run = IngestedCrewRun(
        run_id="crew-fail-closed-1",
        crew_id="crew-fail",
        roles=(
            CrewRole(role_id="lead", name="Lead"),
            CrewRole(role_id="worker", name="Worker"),
        ),
        tasks=(CrewTask(task_id="t1", description="task 1", assigned_role="lead"),),
        handoffs=(
            CrewHandoff(
                from_role="lead",
                to_role="worker",
                parent_handoff_index=42,
            ),
        ),
    )
    with pytest.raises(ValueError, match="references non-existent parent handoff index 42"):
        adapter.record_handoffs_to_ledger(ledger, invalid_index_run)

    # 2. Missing parent HMAC ref
    missing_parent_ref_run = IngestedCrewRun(
        run_id="crew-fail-closed-2",
        crew_id="crew-fail",
        roles=(
            CrewRole(role_id="lead", name="Lead"),
            CrewRole(role_id="worker", name="Worker"),
        ),
        tasks=(CrewTask(task_id="t1", description="task 1", assigned_role="lead"),),
        handoffs=(
            CrewHandoff(
                from_role="lead",
                to_role="worker",
                parent_ref="deadbeef" * 8,
            ),
        ),
    )
    with pytest.raises(ValueError, match="references missing parent receipt HMAC"):
        adapter.record_handoffs_to_ledger(ledger, missing_parent_ref_run)

    # 3. Root handoff requiring parent fails closed
    root_requires_parent_run = IngestedCrewRun(
        run_id="crew-fail-closed-3",
        crew_id="crew-fail",
        roles=(
            CrewRole(role_id="lead", name="Lead"),
            CrewRole(role_id="worker", name="Worker"),
        ),
        tasks=(CrewTask(task_id="t1", description="task 1", assigned_role="lead"),),
        handoffs=(
            CrewHandoff(
                from_role="lead",
                to_role="worker",
                requires_parent=True,
            ),
        ),
    )
    with pytest.raises(ValueError, match="requires parent receipt but has none"):
        adapter.record_handoffs_to_ledger(ledger, root_requires_parent_run)


def test_reingesting_same_fixture_is_idempotent(
    adapter: CrewIngestAdapter,
    ledger: DelegationLedger,
) -> None:
    run = adapter.ingest_trace(FIXTURE_PATH)
    receipts_first = adapter.record_handoffs_to_ledger(ledger, run)
    assert len(receipts_first) == 2

    receipts_second = adapter.record_handoffs_to_ledger(ledger, run)
    assert len(receipts_second) == 2
    assert [r.hmac for r in receipts_first] == [r.hmac for r in receipts_second]

    # File contains exactly 2 receipts, not 4
    path = ledger.receipt_path(run.run_id)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2


def test_crew_callback_handler_records_trace(adapter: CrewIngestAdapter, ledger: DelegationLedger) -> None:
    handler = CrewCallbackHandler(run_id="live-crew-run", crew_id="live-crew")
    handler.on_role_start("planner", name="Planner", goal="Plan execution", backstory="Lead planner")
    handler.on_role_start("coder", name="Coder", goal="Write code", backstory="Expert developer")

    handler.on_task_start("plan-task", description="Produce design", role_id="planner")
    handler.on_tool_call(
        "plan-task",
        tool_name="read_spec",
        call_id="call-10",
        args={"file": "spec.md"},
        output="spec content",
    )
    handler.on_task_finish("plan-task", output="design.md created")

    handler.on_handoff(from_role="planner", to_role="coder", task_id="code-task")

    handler.on_task_start("code-task", description="Implement design", role_id="coder")
    handler.on_tool_call(
        "code-task",
        tool_name="file_writer",
        call_id="call-11",
        args={"file": "app.py"},
        output="written",
    )
    handler.on_task_finish("code-task", output="done")

    trace = handler.to_trace_dict()
    assert trace["run_id"] == "live-crew-run"
    assert len(trace["roles"]) == 2
    assert len(trace["tasks"]) == 2
    assert len(trace["handoffs"]) == 1

    run = adapter.ingest_trace(trace)
    assert len(run.roles) == 2
    assert len(run.tasks) == 2
    assert len(run.handoffs) == 1

    receipts = adapter.record_handoffs_to_ledger(ledger, run)
    assert len(receipts) == 1
    assert receipts[0].issuer == "role:planner"
    assert receipts[0].subject == "role:coder"

    verify_res = adapter.verify_run(ledger, "live-crew-run", key=TEST_KEY)
    assert verify_res.valid is True


def test_plugin_contract_integration() -> None:
    plugin = CrewIngestPlugin()
    declaration = plugin.provide_ingest_adapter()
    assert declaration.name == "crew"
    assert declaration.version == "1.0.0"
    assert "gen_ai_activity" in declaration.declared_event_types
    assert "untyped_activity" in declaration.declared_event_types

    adapter = plugin.get_adapter()
    assert isinstance(adapter, CrewIngestAdapter)
    adapter.validate_declaration(declaration)

    # Invalid declaration rejected
    bad_dec = IngestAdapterDeclaration(
        name="crew",
        version="1.0.0",
        declared_event_types=("non_existent_activity_type",),
        summary="bad",
    )
    with pytest.raises(ValueError, match="unknown event types"):
        adapter.validate_declaration(bad_dec)


def test_ingest_validation_errors(adapter: CrewIngestAdapter) -> None:
    with pytest.raises(ValueError, match="must contain a 'roles' list"):
        adapter.ingest_trace({"run_id": "test"})

    with pytest.raises(ValueError, match="must contain a 'tasks' list"):
        adapter.ingest_trace({"run_id": "test", "roles": [{"role_id": "r1"}]})

    with pytest.raises(ValueError, match="missing 'role_id' or 'name'"):
        adapter.ingest_trace({"run_id": "test", "roles": [{}], "tasks": []})

    with pytest.raises(ValueError, match="missing 'task_id'"):
        adapter.ingest_trace(
            {
                "run_id": "test",
                "roles": [{"role_id": "r1"}],
                "tasks": [{}],
            }
        )

    with pytest.raises(TypeError, match="Unsupported data type"):
        adapter.ingest_trace(12345)  # type: ignore[arg-type]
