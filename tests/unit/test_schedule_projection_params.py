"""Params inside the deterministic fire projection (#2545, AC1 + AC5).

A parameterized fire folds a ``params_hash`` into the projection following the
``trigger_input_hash`` precedent: conditional so a params-less fire stays
byte-identical to a pre-#2545 projection, and load-bearing so two operators
with equal params derive the byte-identical graph while a changed param
provably changes the graph hash.
"""

from __future__ import annotations

from bernstein.core.orchestration.schedule_projection import project_schedule_fire
from bernstein.core.tasks.param_contract import ParamContract

_CONTRACT = ParamContract.from_schema(
    [
        {"name": "target", "type": "string", "required": True},
        {"name": "retries", "type": "int", "default": 3},
    ]
)


def _hash(**overrides: str) -> str:
    validated = _CONTRACT.validate_and_coerce(overrides)
    return _CONTRACT.params_hash(validated)


def test_params_absent_is_byte_identical_to_prior_rev() -> None:
    # AC5: a params-less fire must be byte-identical to a fire that never knew
    # about params (empty params_hash folds nothing).
    without = project_schedule_fire(schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="nightly")
    empty = project_schedule_fire(
        schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="nightly", params_hash=""
    )
    assert without.canonical_bytes == empty.canonical_bytes
    assert without.projection_hash == empty.projection_hash
    assert "params_hash" not in without.to_dict()


def test_two_operators_equal_params_fire_identical_graph() -> None:
    # AC1: identical (schedule_id, fire_time, last_state, params) => identical bytes.
    ph = _hash(target="svc-a")
    op1 = project_schedule_fire(schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=ph)
    op2 = project_schedule_fire(schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=ph)
    assert op1.canonical_bytes == op2.canonical_bytes
    assert op1.projection_hash == op2.projection_hash
    assert op1.params_hash == ph
    assert op1.to_dict()["params_hash"] == ph


def test_changed_param_changes_projection_hash() -> None:
    # AC1: a single changed validated param changes projection_hash.
    a = project_schedule_fire(
        schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=_hash(target="svc-a")
    )
    b = project_schedule_fire(
        schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=_hash(target="svc-b")
    )
    assert a.projection_hash != b.projection_hash


def test_params_hash_binds_task_id_seed() -> None:
    # The task id (not just the canonical payload) is seeded by the params hash,
    # so a re-parameterized nightly run addresses a distinct task graph.
    a = project_schedule_fire(
        schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=_hash(target="a")
    )
    b = project_schedule_fire(
        schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=_hash(target="b")
    )
    assert a.nodes[0].task_id != b.nodes[0].task_id


def test_params_hash_present_in_node_metadata() -> None:
    ph = _hash(target="svc")
    proj = project_schedule_fire(schedule_id="s", fire_time=1_700_000_000, last_state=None, goal="g", params_hash=ph)
    meta = dict(proj.nodes[0].metadata)
    assert meta["params_hash"] == ph
