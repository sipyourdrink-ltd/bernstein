"""Unit tests for collaborative plan editing (CRDT model layer)."""

from __future__ import annotations

import pytest
from bernstein.core.collaborative_plan import (
    CollaborativePlan,
    EditOperation,
    PlanVersion,
    format_edit_summary,
    merge_concurrent_ops,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_PLAN: dict = {
    "name": "test-plan",
    "stages": [
        {"name": "build", "steps": [{"goal": "compile"}]},
        {"name": "test", "steps": [{"goal": "run tests"}]},
    ],
    "metadata": {"owner": "alice", "version": 1},
}


def _make_op(
    op_type: str = "update",
    path: str = "name",
    value: object = "new-name",
    author: str = "alice",
    timestamp: float = 1_000_000.0,
    op_id: str = "op1",
) -> EditOperation:
    return EditOperation(
        op_type=op_type,  # type: ignore[arg-type]
        path=path,
        value=value,
        author=author,
        timestamp=timestamp,
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# EditOperation dataclass
# ---------------------------------------------------------------------------


class TestEditOperation:
    def test_frozen(self) -> None:
        op = _make_op()
        with pytest.raises(AttributeError):
            op.path = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        op = EditOperation(op_type="insert", path="x", value=1, author="bob")
        assert op.timestamp > 0
        assert len(op.op_id) == 32  # uuid4 hex

    def test_fields(self) -> None:
        op = _make_op()
        assert op.op_type == "update"
        assert op.path == "name"
        assert op.value == "new-name"
        assert op.author == "alice"


# ---------------------------------------------------------------------------
# PlanVersion dataclass
# ---------------------------------------------------------------------------


class TestPlanVersion:
    def test_frozen(self) -> None:
        pv = PlanVersion(version=1, ops=[], snapshot={})
        with pytest.raises(AttributeError):
            pv.version = 2  # type: ignore[misc]

    def test_fields(self) -> None:
        op = _make_op()
        pv = PlanVersion(version=3, ops=[op], snapshot={"a": 1})
        assert pv.version == 3
        assert pv.ops == [op]
        assert pv.snapshot == {"a": 1}


# ---------------------------------------------------------------------------
# CollaborativePlan
# ---------------------------------------------------------------------------


class TestCollaborativePlan:
    def test_initial_state(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        assert plan.get_version() == 0
        assert plan.get_snapshot() == _SAMPLE_PLAN
        assert plan.get_authors() == []

    def test_snapshot_is_deep_copy(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        snap = plan.get_snapshot()
        snap["name"] = "mutated"
        assert plan.get_snapshot()["name"] == "test-plan"

    def test_apply_update(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="update", path="name", value="renamed")
        assert plan.apply_op(op) is True
        assert plan.get_version() == 1
        assert plan.get_snapshot()["name"] == "renamed"
        assert plan.get_authors() == ["alice"]

    def test_apply_insert(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="insert", path="metadata.tag", value="v2")
        assert plan.apply_op(op) is True
        assert plan.get_snapshot()["metadata"]["tag"] == "v2"

    def test_apply_delete(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="delete", path="metadata.owner", value=None)
        assert plan.apply_op(op) is True
        assert "owner" not in plan.get_snapshot()["metadata"]

    def test_apply_nested_update(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="update", path="stages.0.name", value="compile")
        assert plan.apply_op(op) is True
        assert plan.get_snapshot()["stages"][0]["name"] == "compile"

    def test_apply_update_missing_path_returns_false(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="update", path="nonexistent.key", value="x")
        assert plan.apply_op(op) is False
        assert plan.get_version() == 0

    def test_apply_delete_missing_path_returns_false(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="delete", path="metadata.missing", value=None)
        assert plan.apply_op(op) is False

    def test_multiple_authors(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        plan.apply_op(_make_op(author="alice"))
        plan.apply_op(_make_op(author="bob", op_id="op2"))
        plan.apply_op(_make_op(author="alice", op_id="op3"))
        assert plan.get_authors() == ["alice", "bob"]

    def test_get_ops_since(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op1 = _make_op(op_id="op1")
        op2 = _make_op(op_id="op2", timestamp=2_000_000.0)
        plan.apply_op(op1)
        plan.apply_op(op2)
        # Ops since version 0 => both
        assert len(plan.get_ops_since(0)) == 2
        # Ops since version 1 => only op2
        ops = plan.get_ops_since(1)
        assert len(ops) == 1
        assert ops[0].op_id == "op2"
        # Ops since current version => empty
        assert plan.get_ops_since(2) == []

    def test_version_increments_only_on_success(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        plan.apply_op(_make_op())  # success
        plan.apply_op(_make_op(op_type="delete", path="no.such", op_id="op2"))  # fail
        assert plan.get_version() == 1

    def test_initial_plan_not_mutated(self) -> None:
        original = {"name": "orig"}
        plan = CollaborativePlan(original)
        plan.apply_op(_make_op(op_type="update", path="name", value="changed"))
        assert original["name"] == "orig"

    def test_list_index_delete(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="delete", path="stages.1", value=None)
        assert plan.apply_op(op) is True
        assert len(plan.get_snapshot()["stages"]) == 1

    def test_list_index_out_of_range(self) -> None:
        plan = CollaborativePlan(_SAMPLE_PLAN)
        op = _make_op(op_type="update", path="stages.99.name", value="x")
        assert plan.apply_op(op) is False


# ---------------------------------------------------------------------------
# merge_concurrent_ops
# ---------------------------------------------------------------------------


class TestMergeConcurrentOps:
    def test_disjoint_paths(self) -> None:
        a = [_make_op(path="name", value="a", timestamp=1.0)]
        b = [_make_op(path="metadata.owner", value="b", timestamp=2.0, op_id="op2")]
        merged = merge_concurrent_ops(a, b)
        assert len(merged) == 2
        paths = {op.path for op in merged}
        assert paths == {"name", "metadata.owner"}

    def test_lww_b_wins_later_timestamp(self) -> None:
        a = [_make_op(path="name", value="a-val", timestamp=1.0)]
        b = [_make_op(path="name", value="b-val", timestamp=2.0, op_id="op2")]
        merged = merge_concurrent_ops(a, b)
        assert len(merged) == 1
        assert merged[0].value == "b-val"

    def test_lww_a_wins_later_timestamp(self) -> None:
        a = [_make_op(path="name", value="a-val", timestamp=3.0)]
        b = [_make_op(path="name", value="b-val", timestamp=1.0, op_id="op2")]
        merged = merge_concurrent_ops(a, b)
        assert len(merged) == 1
        assert merged[0].value == "a-val"

    def test_lww_tie_b_wins(self) -> None:
        a = [_make_op(path="name", value="a-val", timestamp=1.0)]
        b = [_make_op(path="name", value="b-val", timestamp=1.0, op_id="op2")]
        merged = merge_concurrent_ops(a, b)
        assert len(merged) == 1
        assert merged[0].value == "b-val"

    def test_empty_inputs(self) -> None:
        assert merge_concurrent_ops([], []) == []

    def test_one_side_empty(self) -> None:
        ops = [_make_op()]
        assert merge_concurrent_ops(ops, []) == ops
        assert merge_concurrent_ops([], ops) == ops

    def test_sorted_by_timestamp(self) -> None:
        a = [_make_op(path="z", timestamp=10.0)]
        b = [_make_op(path="a", timestamp=1.0, op_id="op2")]
        merged = merge_concurrent_ops(a, b)
        assert merged[0].path == "a"
        assert merged[1].path == "z"

    def test_multiple_ops_per_side(self) -> None:
        a = [
            _make_op(path="name", value="a1", timestamp=1.0, op_id="a1"),
            _make_op(path="metadata.owner", value="a2", timestamp=3.0, op_id="a2"),
        ]
        b = [
            _make_op(path="name", value="b1", timestamp=2.0, op_id="b1"),
            _make_op(path="metadata.version", value=2, timestamp=1.0, op_id="b2"),
        ]
        merged = merge_concurrent_ops(a, b)
        assert len(merged) == 3
        by_path = {op.path: op for op in merged}
        assert by_path["name"].value == "b1"  # b wins (later ts)
        assert by_path["metadata.owner"].value == "a2"
        assert by_path["metadata.version"].value == 2


# ---------------------------------------------------------------------------
# format_edit_summary
# ---------------------------------------------------------------------------


class TestFormatEditSummary:
    def test_empty(self) -> None:
        assert format_edit_summary([]) == "No edits."

    def test_update(self) -> None:
        ops = [_make_op(op_type="update", path="name", value="new", timestamp=1.0)]
        result = format_edit_summary(ops)
        assert "[update]" in result
        assert "name = 'new'" in result
        assert "alice" in result

    def test_delete(self) -> None:
        ops = [_make_op(op_type="delete", path="stages.1", value=None, timestamp=2.0)]
        result = format_edit_summary(ops)
        assert "[delete]" in result
        assert "stages.1" in result
        assert "= " not in result.split("[delete]")[1].split("(")[0]

    def test_insert(self) -> None:
        ops = [_make_op(op_type="insert", path="new_key", value=42, timestamp=3.0)]
        result = format_edit_summary(ops)
        assert "[insert]" in result
        assert "new_key = 42" in result

    def test_multiple_ops(self) -> None:
        ops = [
            _make_op(op_type="update", path="a", value=1, timestamp=1.0, op_id="o1"),
            _make_op(op_type="delete", path="b", value=None, timestamp=2.0, op_id="o2"),
        ]
        result = format_edit_summary(ops)
        lines = result.strip().split("\n")
        assert len(lines) == 2
