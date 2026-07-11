"""Integration: routing emits well-formed decision-log records.

These tests boot the *real* :func:`bernstein.core.routing.route_task`
function against an in-memory ``Task`` and confirm that:

* every routing call appends exactly one record,
* the kind is ``model_route``,
* the ``inputs`` payload carries the task id and role,
* the ``BERNSTEIN_DECISION_LOG=0`` guard suppresses the writer,
* the CLI ``decisions tail`` subcommand surfaces those records.

Integration scope = "real producer, real writer, real reader". The
only stub is the ledger destination path (we point it at a temp file
so the suite does not litter the repo's ``.sdd/runtime/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import Task
from click.testing import CliRunner

from bernstein.core.observability import decision_log as dl


def _make_task(task_id: str = "t-1", role: str = "backend", priority: int = 2) -> Task:
    """Build a minimal Task for routing tests."""
    return Task(
        id=task_id,
        title="Test task",
        description="A small unit of work used to exercise the router.",
        role=role,
        priority=priority,
    )


@pytest.fixture(autouse=True)
def _redirect_decision_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the writer at a tmp file so the repo's .sdd/runtime is untouched.

    We monkeypatch :data:`DEFAULT_PATH` directly because the router calls
    :func:`record_decision` with no explicit path argument.
    """
    monkeypatch.delenv(dl.ENV_DISABLE, raising=False)
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(dl, "DEFAULT_PATH", path)
    return path


def test_routing_emits_decision_record(_redirect_decision_log: Path) -> None:
    """``route_task`` produces exactly one model_route decision per call."""
    from bernstein.core.routing.router_core import route_task

    route_task(_make_task("t-1"), default_model="run-default")

    records = dl.replay(_redirect_decision_log)
    assert len(records) == 1
    assert records[0].kind == "model_route"
    assert records[0].chosen != ""


def test_routing_decision_inputs_carry_task_id(
    _redirect_decision_log: Path,
) -> None:
    """The decision record's ``inputs`` payload includes the task id + role."""
    from bernstein.core.routing.router_core import route_task

    route_task(_make_task("t-42", role="manager"), default_model="run-default")

    [rec] = dl.replay(_redirect_decision_log)
    assert rec.inputs.get("task_id") == "t-42"
    assert rec.inputs.get("role") == "manager"


def test_routing_disable_via_env(_redirect_decision_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``BERNSTEIN_DECISION_LOG=0`` suppresses the writer entirely."""
    from bernstein.core.routing.router_core import route_task

    monkeypatch.setenv(dl.ENV_DISABLE, "0")
    route_task(_make_task("t-3"), default_model="run-default")

    assert not _redirect_decision_log.exists()


def test_routing_multiple_calls_each_emit_one_record(
    _redirect_decision_log: Path,
) -> None:
    """N route_task calls → exactly N records, in call order."""
    from bernstein.core.routing.router_core import route_task

    for i in range(5):
        route_task(_make_task(f"t-{i}"), default_model="run-default")

    records = dl.replay(_redirect_decision_log)
    assert len(records) == 5
    assert [r.inputs.get("task_id") for r in records] == [f"t-{i}" for i in range(5)]


def test_cli_tail_surfaces_routing_decisions(
    _redirect_decision_log: Path,
) -> None:
    """``bernstein decisions tail`` prints rows for the routing decisions."""
    from bernstein.cli.commands.decisions_cmd import decisions_group
    from bernstein.core.routing.router_core import route_task

    route_task(_make_task("t-cli-1", role="frontend"), default_model="run-default")
    route_task(_make_task("t-cli-2", role="backend"), default_model="run-default")

    runner = CliRunner()
    result = runner.invoke(decisions_group, ["tail", "--path", str(_redirect_decision_log)])
    assert result.exit_code == 0, result.output
    assert "model_route" in result.output
