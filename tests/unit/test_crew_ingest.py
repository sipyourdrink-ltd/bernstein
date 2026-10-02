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


def test_tool_call_non_serializable_args_handled_gracefully() -> None:
    """Non-serializable argument objects (e.g. datetime) serialize gracefully without error."""
    import datetime

    from bernstein.adapters.crew_ingest import CrewToolCall

    now = datetime.datetime(2026, 9, 12, 10, 0, 0)
    tc = CrewToolCall.from_dict(
        {
            "name": "calendar_query",
            "id": "tc-date-1",
            "args": {"time": now, "items": [1, 2]},
        }
    )
    assert "2026-09-12" in tc.arguments_digest or tc.arguments_digest.startswith("sha256:")


def test_crew_callback_handler_handles_repeated_task_ids() -> None:
    """Repeated task starts with the same task ID (retries/loops) preserve all executions."""
    handler = CrewCallbackHandler(run_id="run-retry", crew_id="retry-crew")
    handler.on_task_start(task_id="retry-task", description="attempt 1", role_id="worker")
    handler.on_tool_call(task_id="retry-task", tool_name="tool_a", call_id="c1", args={"attempt": 1})
    handler.on_task_complete(task_id="retry-task", output="failed")

    handler.on_task_start(task_id="retry-task", description="attempt 2", role_id="worker")
    handler.on_tool_call(task_id="retry-task", tool_name="tool_a", call_id="c2", args={"attempt": 2})
    handler.on_task_complete(task_id="retry-task", output="success")

    trace = handler.export_trace()
    assert len(trace["tasks"]) == 2
    assert trace["tasks"][0]["task_id"] == "retry-task"
    assert trace["tasks"][1]["task_id"] == "retry-task:1"
    assert trace["tasks"][0]["output"] == "failed"
    assert trace["tasks"][1]["output"] == "success"


def test_rejected_trace_leaves_no_receipt_on_disk(adapter: CrewIngestAdapter, ledger: DelegationLedger) -> None:
    """A trace rejected at a later handoff must append nothing to the ledger."""
    run = IngestedCrewRun(
        run_id="crew-rejected-atomic",
        crew_id="crew-rejected",
        roles=(
            CrewRole(role_id="lead", name="Lead"),
            CrewRole(role_id="worker", name="Worker"),
            CrewRole(role_id="auditor", name="Auditor"),
        ),
        tasks=(CrewTask(task_id="t1", description="task 1", assigned_role="lead"),),
        handoffs=(
            CrewHandoff(from_role="lead", to_role="worker", timestamp=1000),
            CrewHandoff(from_role="worker", to_role="auditor", timestamp=2000, parent_handoff_index=42),
        ),
    )
    with pytest.raises(ValueError, match="non-existent parent handoff index 42"):
        adapter.record_handoffs_to_ledger(ledger, run)
    assert not ledger.receipt_path(run.run_id).exists()


def test_undeclared_from_role_rejected(adapter: CrewIngestAdapter, ledger: DelegationLedger) -> None:
    """A handoff whose from_role is not a declared role fails closed."""
    run = IngestedCrewRun(
        run_id="crew-ghost",
        crew_id="crew-ghost",
        roles=(CrewRole(role_id="lead", name="Lead"), CrewRole(role_id="worker", name="Worker")),
        tasks=(CrewTask(task_id="t1", description="task 1", assigned_role="lead"),),
        handoffs=(CrewHandoff(from_role="ghost", to_role="worker", timestamp=1000),),
    )
    with pytest.raises(ValueError, match="not a declared role"):
        adapter.record_handoffs_to_ledger(ledger, run)


def test_non_root_handoff_without_parent_rejected(adapter: CrewIngestAdapter, ledger: DelegationLedger) -> None:
    """A non-root handoff with no resolvable parent fails closed."""
    run = IngestedCrewRun(
        run_id="crew-no-parent",
        crew_id="crew-no-parent",
        roles=(CrewRole(role_id="lead", name="Lead"), CrewRole(role_id="worker", name="Worker")),
        tasks=(CrewTask(task_id="t1", description="task 1", assigned_role="lead"),),
        handoffs=(
            CrewHandoff(from_role="lead", to_role="worker", timestamp=1000),
            CrewHandoff(from_role="worker", to_role="lead", timestamp=2000),
        ),
    )
    with pytest.raises(ValueError, match="has no resolvable parent"):
        adapter.record_handoffs_to_ledger(ledger, run)


def test_mismatched_digest_rejected() -> None:
    """A trace-supplied arguments_digest that does not match is rejected."""
    from bernstein.adapters.crew_ingest import CrewToolCall

    with pytest.raises(ValueError, match="digest mismatch"):
        CrewToolCall.from_dict(
            {
                "name": "web_search",
                "id": "call-1",
                "args": {"query": "agent governance"},
                "arguments_digest": "sha256:lie",
            }
        )


def test_non_dict_handoff_and_tool_call_rejected(adapter: CrewIngestAdapter) -> None:
    """Malformed handoff/tool-call entries raise instead of being silently dropped."""
    with pytest.raises(ValueError, match="Handoff at index 1 must be a dictionary"):
        adapter.ingest_trace(
            {
                "run_id": "crew-malformed",
                "roles": [{"role_id": "lead"}],
                "tasks": [{"task_id": "t1"}],
                "handoffs": [{"from_role": "lead", "to_role": "lead"}, "garbage"],
            }
        )

    with pytest.raises(ValueError, match="Tool call at index 1 must be a dictionary"):
        adapter.ingest_trace(
            {
                "run_id": "crew-malformed-tool",
                "roles": [{"role_id": "lead"}],
                "tasks": [
                    {
                        "task_id": "t1",
                        "assigned_role": "lead",
                        "tool_calls": [{"name": "a", "id": "c1", "args": {}}, 42],
                    }
                ],
            }
        )


def test_missing_run_id_rejected(adapter: CrewIngestAdapter) -> None:
    """A trace without a run_id is rejected instead of defaulting to a shared id."""
    with pytest.raises(ValueError, match="missing 'run_id'"):
        adapter.ingest_trace({"roles": [{"role_id": "lead"}], "tasks": []})
