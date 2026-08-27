"""The parked-session store gets a producer (#3453).

``load_parked_sessions()`` and its ``available`` flag landed first, but
nothing ever wrote the file they read, so every surface built on them --
``bernstein status``, ``bernstein agents parked``, ``bernstein fleet``,
the TUI status bar -- reported "nothing parked" unconditionally and
always. These tests cover the writer end: a park reaches disk, a
*different* supervisor instance (standing in for the CLI process, which
is never the process that parked anything) can read it, and an operator
resume clears it.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.agents.spawn_supervisor import (
    PARKED_STORE_RELPATH,
    RespawnBudget,
    SpawnSupervisor,
    SupervisorState,
)
from bernstein.core.orchestration.supervisor_aggregator import (
    load_parked_sessions,
    observed_parked_sessions,
)


def _store(workdir: Path) -> Path:
    return workdir.joinpath(*PARKED_STORE_RELPATH)


def _budget(max_respawns: int = 2) -> RespawnBudget:
    """A budget with no real sleeping in it."""
    return RespawnBudget(max_respawns=max_respawns, initial_backoff_ms=0, max_backoff_ms=0)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_exhausting_the_budget_writes_the_path_the_aggregator_reads(tmp_path: Path) -> None:
    """The park lands at the exact path load_parked_sessions() looks at.

    This is the join the issue is about: the reader was pointed at
    ``.sdd/runtime/spawn_supervisor/parked.json`` and nothing put a file
    there. Asserted through the reader rather than by re-deriving the
    path, so the two cannot drift apart silently.
    """
    sup = SpawnSupervisor(workdir=tmp_path)
    budget = _budget(max_respawns=2)

    assert sup.record_spawn_failure("batch:T-1", RuntimeError("boom"), budget=budget) is True
    assert sup.record_spawn_failure("batch:T-1", RuntimeError("boom")) is True
    # Third failure exhausts the budget of two respawns.
    assert sup.record_spawn_failure("batch:T-1", RuntimeError("boom")) is False

    assert sup.is_parked("batch:T-1")
    assert _store(tmp_path).exists()

    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset({"batch:T-1"})


def test_a_fresh_supervisor_sees_a_park_made_by_another_one(tmp_path: Path) -> None:
    """The CLI case: a process that never supervised anything still sees the park.

    ``bernstein agents parked`` runs in its own process with an empty
    in-memory supervisor. Reading only that supervisor is what made the
    surface report zero by construction.
    """
    orchestrator_side = SpawnSupervisor(workdir=tmp_path)
    orchestrator_side.park("batch:T-9", reason="crash loop")

    cli_side = SpawnSupervisor(workdir=tmp_path)
    assert cli_side.parked_sessions() == [], "the fresh supervisor has no memory of the park"

    observed = observed_parked_sessions(tmp_path, in_process=cli_side.parked_sessions())
    assert observed.available
    assert observed.session_ids == frozenset({"batch:T-9"})


def test_budget_remaining_does_not_park_or_claim_a_park(tmp_path: Path) -> None:
    """A failure inside budget records a respawn and writes no parked id."""
    sup = SpawnSupervisor(workdir=tmp_path)

    assert sup.record_spawn_failure("batch:T-2", RuntimeError("flake"), budget=_budget(3)) is True

    assert sup.state("batch:T-2") == SupervisorState.RESPAWNING
    result = load_parked_sessions(tmp_path)
    assert result.available, "the supervisor ran, so the store speaks for this workspace"
    assert result.session_ids == frozenset(), "in-budget failures are not parks"


def test_record_spawn_failure_does_not_sleep(tmp_path: Path) -> None:
    """The non-blocking path must not apply the backoff schedule.

    The orchestrator calls this from inside its tick; a blocking sleep
    here would stall the loop and duplicate the retry schedule the tick
    already owns.
    """
    slept: list[float] = []
    sup = SpawnSupervisor(workdir=tmp_path, sleep=slept.append)

    for _ in range(4):
        sup.record_spawn_failure("batch:T-3", RuntimeError("boom"), budget=_budget(2))

    assert slept == []


# ---------------------------------------------------------------------------
# Empty vs unavailable, from the producer's side
# ---------------------------------------------------------------------------


def test_a_clean_spawn_makes_zero_a_supported_claim(tmp_path: Path) -> None:
    """A healthy run leaves evidence, so "0 parked" is measured, not assumed."""
    assert not load_parked_sessions(tmp_path).available

    SpawnSupervisor(workdir=tmp_path).note_spawn_success("batch:T-4")

    result = load_parked_sessions(tmp_path)
    assert result.available
    assert result.session_ids == frozenset()


def test_a_supervisor_with_no_workdir_writes_nothing(tmp_path: Path) -> None:
    """Standalone use stays in-process, which is what unit tests want."""
    sup = SpawnSupervisor()
    assert sup.store_path is None

    sup.park("batch:T-5")

    assert sup.is_parked("batch:T-5")
    assert not _store(tmp_path).exists()
    assert not load_parked_sessions(tmp_path).available


# ---------------------------------------------------------------------------
# Resume, across the process boundary
# ---------------------------------------------------------------------------


def test_clear_parked_removes_the_id_a_foreign_supervisor_wrote(tmp_path: Path) -> None:
    """`agents resume` must reach a park this process has no record of."""
    SpawnSupervisor(workdir=tmp_path).park("batch:T-6")

    cli_side = SpawnSupervisor(workdir=tmp_path)
    assert cli_side.resume("batch:T-6") is False, "nothing in memory to resume"
    assert cli_side.clear_parked("batch:T-6") is True

    result = load_parked_sessions(tmp_path)
    assert result.available, "the store still exists; it is simply empty now"
    assert result.session_ids == frozenset()


def test_clear_parked_is_false_for_an_id_that_is_not_parked(tmp_path: Path) -> None:
    """Clearing an unknown id reports that it did nothing."""
    SpawnSupervisor(workdir=tmp_path).park("batch:T-7")

    assert SpawnSupervisor(workdir=tmp_path).clear_parked("batch:nope") is False
    assert load_parked_sessions(tmp_path).session_ids == frozenset({"batch:T-7"})


def test_resume_in_the_owning_process_clears_the_store_too(tmp_path: Path) -> None:
    """An in-process resume must not leave a stale parked id on disk."""
    sup = SpawnSupervisor(workdir=tmp_path)
    sup.park("batch:T-8")
    assert load_parked_sessions(tmp_path).session_ids == frozenset({"batch:T-8"})

    assert sup.resume("batch:T-8") is True

    assert load_parked_sessions(tmp_path).session_ids == frozenset()


# ---------------------------------------------------------------------------
# The store is shared, so writing it must not be destructive
# ---------------------------------------------------------------------------


def test_persisting_does_not_erase_a_park_this_supervisor_never_made(tmp_path: Path) -> None:
    """Two supervisors on one workspace must not overwrite each other.

    A supervisor is authoritative only for the sessions it knows about.
    An overwrite would let any second process silently drop every park it
    had not made itself -- the same class of silent zero this issue is
    about, one layer down.
    """
    first = SpawnSupervisor(workdir=tmp_path)
    first.park("batch:owned-by-first")

    second = SpawnSupervisor(workdir=tmp_path)
    second.park("batch:owned-by-second")

    assert load_parked_sessions(tmp_path).session_ids == frozenset({"batch:owned-by-first", "batch:owned-by-second"})


def test_store_is_valid_json_with_the_documented_keys(tmp_path: Path) -> None:
    """The file the aggregator reads carries the shape it parses."""
    sup = SpawnSupervisor(workdir=tmp_path)
    sup.park("batch:T-shape", reason="missing binary")

    payload = json.loads(_store(tmp_path).read_text(encoding="utf-8"))

    assert payload["session_ids"] == ["batch:T-shape"]
    assert payload["entries"]["batch:T-shape"]["state"] == "parked"
    assert payload["entries"]["batch:T-shape"]["last_error"] == "missing binary"
    assert isinstance(payload["updated_at"], float)


def test_a_corrupt_store_does_not_stop_a_park_being_recorded(tmp_path: Path) -> None:
    """Supervision survives a damaged file rather than propagating the error."""
    store = _store(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")

    SpawnSupervisor(workdir=tmp_path).park("batch:T-10")

    assert load_parked_sessions(tmp_path).session_ids == frozenset({"batch:T-10"})
