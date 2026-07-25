"""A declared output that never landed leaves an artifact-keyed record (#2559).

Before this, the artifact side of the chain could not tell two very different
situations apart. A task declared ``pkg://pypi/bernstein/3.9.0``, ran, and died
before publishing; and nothing was ever scheduled to publish it at all. Both
left zero spine entries under that key, so ``artifact health`` answered "nothing
has ever produced it" to both, and the operator had to go and correlate run
directories to find out which one it was.

The attempt record closes that. It is a spine entry like any other -- chained,
HMAC-tagged, keyed by the artifact URI -- whose ``step_id`` marks it as an
attempt rather than a production. So:

* "attempted and failed" is a *chain fact*, not an inference from log archaeology;
* it is queryable from the artifact side, which is the whole point of keying
  provenance by URI;
* it can never be mistaken for production -- not by the health verdict, not by
  the completion diff, and not by anything that fires on ``artifact.produced``.

These tests pin all three, plus determinism (identical inputs give byte-identical
records) and fail-open behaviour (reconciliation never raises into a completing
task).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.lineage.artifact_attempt import (
    ATTEMPT_OUTCOME_FAILED,
    ATTEMPT_OUTCOME_INCOMPLETE,
    ATTEMPT_RECORD_VERSION,
    attempt_record_bytes,
    reconcile_declared_outputs,
    record_output_attempt,
)
from bernstein.core.lineage.artifact_events import (
    load_production_events,
    observed_artifact_keys,
    replay_production_events,
)
from bernstein.core.lineage.artifact_health import (
    LEG_FAIL,
    RED,
    artifact_attempts,
    artifact_health_json,
    artifact_log,
    artifact_log_json,
    collect_artifact_state,
)
from bernstein.core.lineage.spine import ARTIFACT_ATTEMPT_STEP_PREFIX, LineageSpine, SpineEntry
from bernstein.core.tasks.models import Task
from bernstein.core.tasks.task_lifecycle import _reconcile_declared_outputs as _reconcile_at_completion
from bernstein.core.trigger_sources.artifact import intended_fires

_KEY = b"h" * 32
_PKG = "pkg://pypi/bernstein/3.9.0"
_PR = "pr://github/sipyourdrink-ltd/bernstein/2559"


def _root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _spine(workdir: Path, run_id: str = "run-1") -> LineageSpine:
    return LineageSpine(_root(workdir), run_id=run_id, hmac_key=_KEY)


def _produce(workdir: Path, uri: str, content: bytes, *, ts: int, run_id: str = "run-1") -> SpineEntry:
    return _spine(workdir, run_id).record_entry(
        artifact_path=uri,
        content=content,
        actor="agent-release",
        step_id="step-1",
        model="claude-opus-5",
        timestamp=ts,
    )


def _reconcile(
    workdir: Path,
    declared: list[str],
    *,
    run_id: str = "run-1",
    task_id: str = "task-42",
    ts: int = 900,
    outcome: str = ATTEMPT_OUTCOME_FAILED,
    reason: str = "",
) -> tuple[str, ...]:
    return reconcile_declared_outputs(
        _root(workdir),
        run_id=run_id,
        declared=declared,
        task_id=task_id,
        actor="agent-release",
        model="claude-opus-5",
        hmac_key=_KEY,
        timestamp=ts,
        outcome=outcome,
        reason=reason,
    )


def _leg(payload: str, name: str) -> dict:
    return next(leg for leg in json.loads(payload)["legs"] if leg["name"] == name)


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_a_declared_output_that_never_landed_writes_an_attempt_record(tmp_path: Path) -> None:
    recorded = _reconcile(tmp_path, [_PKG])

    assert recorded == (_PKG,)
    entries = list(_spine(tmp_path).iter_entries())
    assert len(entries) == 1
    assert entries[0].artifact_path == _PKG
    # The marker, the outcome and the declaring task all live on the entry, so
    # they are covered by its hash and HMAC tag rather than by a side table.
    assert entries[0].step_id == f"{ARTIFACT_ATTEMPT_STEP_PREFIX}{ATTEMPT_OUTCOME_FAILED}:task-42"


def test_a_produced_declared_output_writes_no_attempt_record(tmp_path: Path) -> None:
    """The declaration was honoured; there is nothing to record."""
    _produce(tmp_path, _PKG, b"wheel", ts=100)

    assert _reconcile(tmp_path, [_PKG]) == ()
    assert len(list(_spine(tmp_path).iter_entries())) == 1


def test_only_the_missing_half_of_a_declaration_is_recorded(tmp_path: Path) -> None:
    _produce(tmp_path, _PKG, b"wheel", ts=100)

    assert _reconcile(tmp_path, [_PKG, _PR]) == (_PR,)


def test_production_in_another_run_does_not_satisfy_this_run(tmp_path: Path) -> None:
    """A stale artifact from an earlier run is not this task's output."""
    _produce(tmp_path, _PKG, b"old-wheel", ts=100, run_id="run-0")

    assert _reconcile(tmp_path, [_PKG], run_id="run-1") == (_PKG,)


def test_glob_patterns_are_skipped_because_they_name_no_single_artifact(tmp_path: Path) -> None:
    """``pkg://pypi/bernstein/*`` has no URI to key an attempt under."""
    assert _reconcile(tmp_path, ["pkg://pypi/bernstein/*", "docs/*.md"]) == ()
    assert list(_spine(tmp_path).iter_entries()) == []


def test_the_attempt_payload_carries_the_task_and_the_outcome(tmp_path: Path) -> None:
    entry = record_output_attempt(
        _root(tmp_path),
        run_id="run-1",
        uri=_PKG,
        task_id="task-42",
        actor="agent-release",
        model="claude-opus-5",
        hmac_key=_KEY,
        timestamp=900,
        outcome=ATTEMPT_OUTCOME_FAILED,
        reason="janitor rejected the diff",
    )

    expected = attempt_record_bytes(
        task_id="task-42",
        uri=_PKG,
        outcome=ATTEMPT_OUTCOME_FAILED,
        reason="janitor rejected the diff",
    )
    assert json.loads(expected) == {
        "outcome": ATTEMPT_OUTCOME_FAILED,
        "reason": "janitor rejected the diff",
        "task_id": "task-42",
        "uri": _PKG,
        "v": ATTEMPT_RECORD_VERSION,
    }
    # The entry's content hash covers exactly those bytes, so the record is
    # self-describing: a verifier recomputes it without trusting the writer.
    from bernstein.core.lineage.spine import content_hash_of

    assert entry.content_hash == content_hash_of(expected)


# ---------------------------------------------------------------------------
# Distinguishability -- the acceptance criterion
# ---------------------------------------------------------------------------


def test_attempted_and_failed_is_distinguishable_from_never_attempted(tmp_path: Path) -> None:
    _reconcile(tmp_path, [_PKG])
    (tmp_path / ".sdd").mkdir(exist_ok=True)

    attempted = artifact_health_json(tmp_path, _PKG, hmac_key=_KEY, at=1000)
    never = artifact_health_json(tmp_path, _PR, hmac_key=_KEY, at=1000)

    # Both are red -- neither artifact exists -- but for visibly different reasons.
    assert json.loads(attempted)["verdict"] == RED
    assert json.loads(never)["verdict"] == RED
    assert _leg(attempted, "produced")["status"] == LEG_FAIL
    assert _leg(never, "produced")["status"] == LEG_FAIL
    assert _leg(attempted, "produced")["detail"] != _leg(never, "produced")["detail"]

    assert json.loads(attempted)["attempt_count"] == 1
    assert json.loads(never)["attempt_count"] == 0
    assert json.loads(attempted)["last_attempt_at"] == 900
    assert json.loads(never)["last_attempt_at"] is None
    assert "task-42" in _leg(attempted, "produced")["detail"]


def test_the_attempt_is_queryable_from_the_artifact_side(tmp_path: Path) -> None:
    _reconcile(tmp_path, [_PKG], reason="publish step timed out")

    attempts = artifact_attempts(tmp_path, _PKG, hmac_key=_KEY)
    assert len(attempts) == 1
    assert attempts[0].task_id == "task-42"
    assert attempts[0].outcome == ATTEMPT_OUTCOME_FAILED
    assert attempts[0].actor == "agent-release"
    assert attempts[0].model == "claude-opus-5"
    assert attempts[0].verified is True

    payload = json.loads(artifact_log_json(artifact_log(tmp_path, _PKG, hmac_key=_KEY), uri=_PKG, attempts=attempts))
    assert payload["productions"] == []
    assert payload["attempts"][0]["task_id"] == "task-42"


def test_a_later_production_leaves_the_earlier_attempt_visible(tmp_path: Path) -> None:
    """The failed attempt stays on the chain after a retry succeeds."""
    _reconcile(tmp_path, [_PKG], task_id="task-42", ts=900)
    _produce(tmp_path, _PKG, b"wheel", ts=1000)

    verdict = json.loads(artifact_health_json(tmp_path, _PKG, hmac_key=_KEY, at=1100))
    assert verdict["production_count"] == 1
    assert verdict["attempt_count"] == 1
    assert verdict["tip"]["content_hash"] != ""


# ---------------------------------------------------------------------------
# An attempt is never mistaken for a production
# ---------------------------------------------------------------------------


def test_an_attempt_is_not_counted_as_a_production(tmp_path: Path) -> None:
    _reconcile(tmp_path, [_PKG])

    state = collect_artifact_state(tmp_path, _PKG, hmac_key=_KEY)
    assert state.productions == ()
    assert len(state.attempts) == 1
    assert artifact_log(tmp_path, _PKG, hmac_key=_KEY) == ()
    assert json.loads(artifact_health_json(tmp_path, _PKG, hmac_key=_KEY, at=1000))["production_count"] == 0


def test_an_attempt_is_not_an_observed_output(tmp_path: Path) -> None:
    """Otherwise the record of a missing output would satisfy its own declaration."""
    _reconcile(tmp_path, [_PKG])

    assert observed_artifact_keys(_root(tmp_path), run_id="run-1") == ()


def test_an_attempt_fires_no_production_trigger(tmp_path: Path) -> None:
    """Downstream goals react to outputs landing, never to one failing to land."""
    _reconcile(tmp_path, [_PKG])
    _produce(tmp_path, _PR, b"head", ts=1000)

    fired = intended_fires(list(_spine(tmp_path).iter_entries()), run_id="run-1", hmac_key=_KEY)
    assert [e.uri for e in fired] == [_PR]


def test_the_attempt_entry_still_replays_so_the_fan_out_stays_exact(tmp_path: Path) -> None:
    """No opt-in gap: the entry is journaled and replays like any other."""
    _reconcile(tmp_path, [_PKG])

    journaled = load_production_events(_root(tmp_path), run_id="run-1")
    replayed = replay_production_events(_root(tmp_path), run_id="run-1", hmac_key=_KEY)
    assert [e.entry_hash for e in journaled] == [e.entry_hash for e in replayed]
    assert journaled[0].is_attempt is True


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_two_reconciliations_of_the_same_inputs_are_byte_identical(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _reconcile(a, [_PKG, _PR])
    _reconcile(b, [_PR, _PKG])  # declaration order must not matter

    assert (_root(a) / "run-1" / "spine.jsonl").read_bytes() == (_root(b) / "run-1" / "spine.jsonl").read_bytes()


def test_the_attempt_payload_is_independent_of_the_wall_clock(tmp_path: Path) -> None:
    first = attempt_record_bytes(task_id="t", uri=_PKG, outcome=ATTEMPT_OUTCOME_INCOMPLETE, reason="r")
    second = attempt_record_bytes(task_id="t", uri=_PKG, outcome=ATTEMPT_OUTCOME_INCOMPLETE, reason="r")
    assert first == second
    assert b"time" not in first


def test_reconciliation_is_idempotent_for_the_same_task_and_uri(tmp_path: Path) -> None:
    """A reap that runs twice must not stack duplicate attempts on the chain."""
    assert _reconcile(tmp_path, [_PKG]) == (_PKG,)
    assert _reconcile(tmp_path, [_PKG]) == ()

    assert len(list(_spine(tmp_path).iter_entries())) == 1


def test_a_different_task_records_its_own_attempt(tmp_path: Path) -> None:
    _reconcile(tmp_path, [_PKG], task_id="task-1")
    _reconcile(tmp_path, [_PKG], task_id="task-2", ts=950)

    attempts = artifact_attempts(tmp_path, _PKG, hmac_key=_KEY)
    assert sorted(a.task_id for a in attempts) == ["task-1", "task-2"]


# ---------------------------------------------------------------------------
# Tamper
# ---------------------------------------------------------------------------


def test_tampering_an_attempt_record_is_detected(tmp_path: Path) -> None:
    _reconcile(tmp_path, [_PKG], reason="honest reason")
    spine_path = _root(tmp_path) / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["actor"] = "somebody-else"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    assert artifact_attempts(tmp_path, _PKG, hmac_key=_KEY)[0].verified is False
    payload = artifact_health_json(tmp_path, _PKG, hmac_key=_KEY, at=1000)
    assert json.loads(payload)["verdict"] == RED
    assert _leg(payload, "chain_integrity")["status"] == LEG_FAIL


def test_an_attempt_cannot_be_forged_into_a_production(tmp_path: Path) -> None:
    """Rewriting the marker off an attempt breaks the entry hash it was tagged under."""
    _reconcile(tmp_path, [_PKG])
    spine_path = _root(tmp_path) / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["step_id"] = "step-1"  # try to pass the attempt off as a real write
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    state = collect_artifact_state(tmp_path, _PKG, hmac_key=_KEY)
    assert [p.verified for p in state.productions] == [False]
    assert json.loads(artifact_health_json(tmp_path, _PKG, hmac_key=_KEY, at=1000))["verdict"] == RED


# ---------------------------------------------------------------------------
# Fail-open (AC7)
# ---------------------------------------------------------------------------


def test_reconciliation_never_raises_when_the_spine_is_unwritable(tmp_path: Path, monkeypatch) -> None:
    """A completing task must never fail because an attempt record could not land."""

    def _boom(*_args: object, **_kwargs: object) -> SpineEntry:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(LineageSpine, "record_entry", _boom)
    assert _reconcile(tmp_path, [_PKG]) == ()


def test_reconciliation_never_raises_on_an_unreadable_spine(tmp_path: Path, monkeypatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk gone")

    monkeypatch.setattr(LineageSpine, "iter_entries", _boom)
    assert _reconcile(tmp_path, [_PKG]) == ()


def test_reconciliation_never_raises_on_a_malformed_declaration(tmp_path: Path) -> None:
    """Operator input reaches this path; a bad key is skipped, not fatal."""
    assert _reconcile(tmp_path, ["../../etc/passwd", "/absolute/path", "ftp://nope", _PKG]) == (_PKG,)


def test_nothing_declared_is_a_zero_touch_no_op(tmp_path: Path) -> None:
    assert _reconcile(tmp_path, []) == ()
    assert not _root(tmp_path).exists()


@pytest.mark.parametrize("outcome", [ATTEMPT_OUTCOME_FAILED, ATTEMPT_OUTCOME_INCOMPLETE])
def test_both_outcomes_record_and_round_trip(tmp_path: Path, outcome: str) -> None:
    _reconcile(tmp_path, [_PKG], outcome=outcome)
    assert artifact_attempts(tmp_path, _PKG, hmac_key=_KEY)[0].outcome == outcome


# ---------------------------------------------------------------------------
# The completion seam (AC7: fail-open with respect to task completion)
# ---------------------------------------------------------------------------


def _orch(tmp_path: Path) -> Any:
    return SimpleNamespace(_workdir=tmp_path, _recorder=SimpleNamespace(run_id="run-1"))


def _session(agent_id: str = "agent-7") -> Any:
    """The seam reads only ``session.id``; a stand-in keeps the test honest and small."""
    return SimpleNamespace(id=agent_id)


def _task(declared: list[str]) -> Task:
    return Task.from_dict(
        {
            "id": "task-42",
            "title": "publish the wheel",
            "role": "dev",
            "description": "d",
            "declared_outputs": declared,
            "model": "claude-opus-5",
        }
    )


@pytest.fixture
def _pinned_key(monkeypatch) -> None:
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: _KEY)


def test_the_completion_seam_records_a_missing_declared_output(tmp_path: Path, _pinned_key: None) -> None:
    _reconcile_at_completion(_orch(tmp_path), _task([_PKG]), _session(), delivered=False)

    attempts = artifact_attempts(tmp_path, _PKG, hmac_key=_KEY)
    assert len(attempts) == 1
    assert attempts[0].task_id == "task-42"
    assert attempts[0].outcome == ATTEMPT_OUTCOME_FAILED
    assert attempts[0].actor == "agent-7"
    assert attempts[0].model == "claude-opus-5"


def test_a_delivered_task_missing_its_output_records_the_quieter_outcome(tmp_path: Path, _pinned_key: None) -> None:
    """Accepted work with an absent declared output is still a finding."""
    _reconcile_at_completion(_orch(tmp_path), _task([_PKG]), _session(), delivered=True)

    assert artifact_attempts(tmp_path, _PKG, hmac_key=_KEY)[0].outcome == ATTEMPT_OUTCOME_INCOMPLETE


def test_the_completion_seam_is_silent_when_the_output_landed(tmp_path: Path, _pinned_key: None) -> None:
    _produce(tmp_path, _PKG, b"wheel", ts=100)
    _reconcile_at_completion(_orch(tmp_path), _task([_PKG]), _session(), delivered=True)

    assert artifact_attempts(tmp_path, _PKG, hmac_key=_KEY) == ()


def test_the_completion_seam_never_raises_into_a_finishing_task(tmp_path: Path, monkeypatch) -> None:
    """Fault injection: every dependency of the seam fails; completion is unaffected.

    This is the failure-domain boundary. The task has already finished when the
    seam runs, so a broken key store, an unwritable spine or a missing recorder
    must cost a record, never the completion.
    """

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected fault")

    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", _boom)
    _reconcile_at_completion(_orch(tmp_path), _task([_PKG]), _session(), delivered=False)

    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: _KEY)
    monkeypatch.setattr(LineageSpine, "record_entry", _boom)
    _reconcile_at_completion(_orch(tmp_path), _task([_PKG]), _session(), delivered=False)


def test_the_completion_seam_is_a_no_op_without_a_run_recorder(tmp_path: Path, _pinned_key: None) -> None:
    orch: Any = SimpleNamespace(_workdir=tmp_path, _recorder=None)
    _reconcile_at_completion(orch, _task([_PKG]), _session(), delivered=False)

    assert not _root(tmp_path).exists()


def test_the_completion_seam_touches_nothing_when_no_output_is_declared(tmp_path: Path, _pinned_key: None) -> None:
    _reconcile_at_completion(_orch(tmp_path), _task([]), _session(), delivered=True)

    assert not _root(tmp_path).exists()
