"""The orchestrator's spawn-failure path feeds the supervisor (#3453).

The read surfaces were complete and the producer was missing:
``SpawnSupervisor`` had no production caller at all, so the respawn
budget could not be consumed outside a unit test and nothing was ever
parked. These tests drive the real ``claim_and_spawn_batches`` failure
path to exhaustion and assert the park reaches the store the aggregator
reads.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import AgentSession, ModelConfig
from bernstein.core.orchestrator import TickResult

from bernstein.core.agents.spawn_supervisor import get_supervisor, reset_supervisor
from bernstein.core.orchestration.supervisor_aggregator import load_parked_sessions
from bernstein.core.tasks.task_lifecycle import (
    _batch_lineage_key,
    _park_key,
    claim_and_spawn_batches,
)


@pytest.fixture(autouse=True)
def _isolate_supervisor() -> Any:
    """The supervisor is process-wide; keep it from leaking between tests."""
    reset_supervisor()
    yield
    reset_supervisor()


def _never_quarantined(*_args: Any, **_kwargs: Any) -> bool:
    return False


def _no_quarantine_entry(*_args: Any, **_kwargs: Any) -> None:
    return None


def _orch(tmp_path: Path) -> Any:
    """Minimal orchestrator stub for the spawn path (mirrors test_task_lifecycle)."""
    client = MagicMock()
    client.post.return_value = SimpleNamespace(status_code=200)
    spawner = MagicMock()
    spawner._adapter = None
    return SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            max_agents=2,
            force_parallel=False,
            max_agent_runtime_s=900,
            ab_test=False,
        ),
        _client=client,
        _spawner=spawner,
        _agents={},
        _file_ownership={},
        _spawn_failures={},
        _quarantine=SimpleNamespace(
            is_quarantined=_never_quarantined,
            get_entry=_no_quarantine_entry,
        ),
        _decomposed_task_ids=set(),
        _idle_shutdown_ts=set(),
        _workdir=tmp_path,
        _response_cache=None,
        _batch_api=None,
        _batch_sessions={},
        _fast_path_stats={},
        _preserved_worktrees={},
        _task_to_session={},
        _SPAWN_BACKOFF_BASE_S=5,
        _SPAWN_BACKOFF_MAX_S=60,
        _MAX_SPAWN_FAILURES=3,
        _lock_manager=None,
        is_shutting_down=lambda: False,
    )


def _tick(orch: Any, batch: list[Any]) -> None:
    """Run one spawn attempt, defeating the inter-attempt backoff.

    The backoff is real and wall-clock based; zeroing the recorded
    timestamp is what lets a unit test reach the third consecutive
    failure without sleeping through two 30s network-error delays.
    """
    key = _batch_lineage_key(batch)
    if key in orch._spawn_failures:
        count, _ = orch._spawn_failures[key]
        orch._spawn_failures[key] = (count, 0.0)
    claim_and_spawn_batches(
        orch,
        [batch],
        alive_count=0,
        assigned_task_ids=set(),
        done_ids=set(),
        result=TickResult(),
    )


def test_repeated_spawn_failure_parks_the_batch_on_disk(tmp_path: Path, make_task: Any) -> None:
    """Three consecutive spawn failures park the work unit where the CLI can see it.

    This is the issue's "Done means" bullet: a real spawn failure driven
    to exhaustion leaves a parked entry at the path the aggregator reads.
    Read back through ``load_parked_sessions`` so the writer and the
    reader are asserted to agree rather than assumed to.
    """
    orch = _orch(tmp_path)
    task = make_task(id="T-park", role="backend")
    orch._spawner.spawn_for_tasks.side_effect = OSError("connection reset by peer")

    assert not load_parked_sessions(tmp_path).available, "nothing has supervised this workspace yet"

    for _ in range(orch._MAX_SPAWN_FAILURES):
        _tick(orch, [task])

    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset({_park_key(_batch_lineage_key([task]))})


def test_a_single_transient_failure_does_not_park(tmp_path: Path, make_task: Any) -> None:
    """One flake is not a crash loop; the surface must not cry parked."""
    orch = _orch(tmp_path)
    task = make_task(id="T-flake", role="backend")
    orch._spawner.spawn_for_tasks.side_effect = OSError("connection reset by peer")

    _tick(orch, [task])

    result = load_parked_sessions(tmp_path)
    assert result.available, "the supervisor ran, so zero is a measured zero"
    assert result.session_ids == frozenset()


def test_a_recovered_batch_leaves_no_parked_entry(tmp_path: Path, make_task: Any) -> None:
    """A batch that fails twice then spawns is not parked, and says so.

    The store is still written -- a healthy run has to leave evidence,
    otherwise "0 parked" is indistinguishable from "nobody watched".
    """
    orch = _orch(tmp_path)
    task = make_task(id="T-recover", role="backend")
    session = AgentSession(
        id="A-recover",
        role="backend",
        task_ids=[task.id],
        model_config=ModelConfig("sonnet", "high"),
    )
    orch._spawner.spawn_for_tasks.side_effect = [
        OSError("connection reset by peer"),
        OSError("connection reset by peer"),
        session,
    ]

    for _ in range(3):
        _tick(orch, [task])

    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset()


def test_the_park_key_survives_a_retry_minting_a_new_task_id(tmp_path: Path, make_task: Any) -> None:
    """The parked id must be lineage-keyed, not per-attempt.

    A retry mints a brand-new task id (#2806), and ``spawner_core`` mints
    a brand-new spawn session id per attempt. Either as the park key
    would make every failure look like a first failure, so the budget
    would never reach exhaustion and nothing would ever park.
    """
    first = make_task(id="T-orig", role="backend")
    retry = make_task(id="T-retry-1", role="backend")
    retry.metadata = {**(retry.metadata or {}), "original_task_id": "T-orig"}

    assert _park_key(_batch_lineage_key([first])) == _park_key(_batch_lineage_key([retry]))


def test_supervisor_is_rooted_at_the_orchestrator_workdir(tmp_path: Path, make_task: Any) -> None:
    """The process supervisor learns where to write from the orchestrator."""
    orch = _orch(tmp_path)
    task = make_task(id="T-root", role="backend")
    orch._spawner.spawn_for_tasks.side_effect = OSError("connection reset by peer")

    _tick(orch, [task])

    assert get_supervisor().store_path == tmp_path.joinpath(".sdd", "runtime", "spawn_supervisor", "parked.json")


def test_an_unreachable_supervisor_is_reported_where_an_operator_looks(
    tmp_path: Path,
    make_task: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A supervisor that cannot be reached leaves a warning, not silence.

    Reaching the supervisor is best-effort on purpose: supervision must
    never take down a spawn. But the failure it guards against looks
    exactly like the defect this wiring exists to fix -- every operator
    surface reporting "nothing parked" unconditionally. Swallowed with no
    trace, the two are indistinguishable, and the run that finds out is
    the one where an operator needed the park and it was not there.

    The spawn itself must still proceed: the assertion is a warning *and*
    a tick that did not raise.
    """
    import logging

    from bernstein.core.tasks import task_lifecycle

    def _unreachable(_orch: Any) -> Any:
        raise RuntimeError("supervisor state directory is read-only")

    monkeypatch.setattr(task_lifecycle, "_spawn_supervisor_for", _unreachable)

    orch = _orch(tmp_path)
    task = make_task(id="T-unreachable", role="backend")
    orch._spawner.spawn_for_tasks.side_effect = OSError("connection reset by peer")

    with caplog.at_level(logging.WARNING, logger="bernstein.core.tasks.task_lifecycle"):
        for _ in range(orch._MAX_SPAWN_FAILURES):
            _tick(orch, [task])

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unreachable supervisor was swallowed without a word"
    assert any("supervisor" in r.getMessage().lower() for r in warnings), (
        f"warned, but not about the supervisor: {[r.getMessage() for r in warnings]}"
    )
    assert any(r.exc_info for r in warnings), "the warning carries no traceback to act on"
