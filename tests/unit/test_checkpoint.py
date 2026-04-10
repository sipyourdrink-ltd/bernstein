"""Tests for orchestrator checkpoint/restore (orch-019)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.checkpoint import (
    Checkpoint,
    CheckpointMetadata,
    create_checkpoint,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
    validate_checkpoint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metadata(
    *,
    checkpoint_id: str = "ckpt-001",
    task_count: int = 10,
    completed_count: int = 5,
    failed_count: int = 1,
    cost_usd: float = 2.50,
    plan_file: str | None = "plans/big-project.yaml",
) -> CheckpointMetadata:
    return CheckpointMetadata(
        checkpoint_id=checkpoint_id,
        created_at="2026-04-10T12:00:00+00:00",
        bernstein_version="1.5.0",
        task_count=task_count,
        completed_count=completed_count,
        failed_count=failed_count,
        cost_usd=cost_usd,
        plan_file=plan_file,
    )


def _make_checkpoint(
    *,
    checkpoint_id: str = "ckpt-001",
    task_count: int = 10,
    completed_count: int = 5,
    failed_count: int = 1,
) -> Checkpoint:
    metadata = _make_metadata(
        checkpoint_id=checkpoint_id,
        task_count=task_count,
        completed_count=completed_count,
        failed_count=failed_count,
    )
    return create_checkpoint(
        metadata=metadata,
        task_graph={"task-1": {"depends_on": []}, "task-2": {"depends_on": ["task-1"]}},
        agent_sessions=[{"agent_id": "a1", "model": "opus", "status": "done"}],
        cost_accumulator={"opus": 1.75, "haiku": 0.75},
        wal_position=42,
    )


# ---------------------------------------------------------------------------
# CheckpointMetadata tests
# ---------------------------------------------------------------------------


class TestCheckpointMetadata:
    """Tests for CheckpointMetadata frozen dataclass."""

    def test_metadata_fields(self) -> None:
        meta = _make_metadata()
        assert meta.checkpoint_id == "ckpt-001"
        assert meta.created_at == "2026-04-10T12:00:00+00:00"
        assert meta.bernstein_version == "1.5.0"
        assert meta.task_count == 10
        assert meta.completed_count == 5
        assert meta.failed_count == 1
        assert meta.cost_usd == 2.50
        assert meta.plan_file == "plans/big-project.yaml"

    def test_metadata_is_frozen(self) -> None:
        meta = _make_metadata()
        with pytest.raises(AttributeError):
            meta.checkpoint_id = "changed"  # type: ignore[misc]

    def test_metadata_none_plan_file(self) -> None:
        meta = _make_metadata(plan_file=None)
        assert meta.plan_file is None


# ---------------------------------------------------------------------------
# create_checkpoint tests
# ---------------------------------------------------------------------------


class TestCreateCheckpoint:
    """Tests for create_checkpoint factory function."""

    def test_creates_frozen_checkpoint(self) -> None:
        ckpt = _make_checkpoint()
        assert isinstance(ckpt, Checkpoint)
        with pytest.raises(AttributeError):
            ckpt.wal_position = 99  # type: ignore[misc]

    def test_checkpoint_fields(self) -> None:
        ckpt = _make_checkpoint()
        assert ckpt.metadata.checkpoint_id == "ckpt-001"
        assert ckpt.task_graph == {
            "task-1": {"depends_on": []},
            "task-2": {"depends_on": ["task-1"]},
        }
        assert len(ckpt.agent_sessions) == 1
        assert ckpt.cost_accumulator["opus"] == 1.75
        assert ckpt.wal_position == 42


# ---------------------------------------------------------------------------
# save / load roundtrip tests
# ---------------------------------------------------------------------------


class TestSaveLoadRoundtrip:
    """Tests for save_checkpoint and load_checkpoint."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        original = _make_checkpoint()
        path = save_checkpoint(original, tmp_path)

        assert path.exists()
        assert path.name == "checkpoint-ckpt-001.json"

        loaded = load_checkpoint(path)
        assert loaded is not None
        assert loaded == original

    def test_roundtrip_none_plan_file(self, tmp_path: Path) -> None:
        meta = _make_metadata(plan_file=None)
        ckpt = create_checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=0,
        )
        path = save_checkpoint(ckpt, tmp_path)
        loaded = load_checkpoint(path)
        assert loaded is not None
        assert loaded.metadata.plan_file is None

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        ckpt = _make_checkpoint()
        path = save_checkpoint(ckpt, nested)
        assert path.exists()

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        ckpt1 = _make_checkpoint(completed_count=3)
        ckpt2 = _make_checkpoint(completed_count=7)
        save_checkpoint(ckpt1, tmp_path)
        path = save_checkpoint(ckpt2, tmp_path)
        loaded = load_checkpoint(path)
        assert loaded is not None
        assert loaded.metadata.completed_count == 7

    def test_saved_json_is_valid(self, tmp_path: Path) -> None:
        ckpt = _make_checkpoint()
        path = save_checkpoint(ckpt, tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "metadata" in data
        assert "task_graph" in data
        assert "wal_position" in data


# ---------------------------------------------------------------------------
# load_checkpoint error cases
# ---------------------------------------------------------------------------


class TestLoadCheckpointErrors:
    """Tests for load_checkpoint with corrupt or missing files."""

    def test_load_missing_file(self, tmp_path: Path) -> None:
        result = load_checkpoint(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_corrupt_json(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "checkpoint-bad.json"
        bad_file.write_text("{this is not json}", encoding="utf-8")
        result = load_checkpoint(bad_file)
        assert result is None

    def test_load_missing_keys(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "checkpoint-missing.json"
        bad_file.write_text('{"metadata": {}}', encoding="utf-8")
        result = load_checkpoint(bad_file)
        assert result is None

    def test_load_wrong_types(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "checkpoint-types.json"
        data = {
            "metadata": {
                "checkpoint_id": 12345,  # should be str
                "created_at": "2026-04-10T12:00:00+00:00",
                "bernstein_version": "1.5.0",
                "task_count": "not-an-int",
                "completed_count": 0,
                "failed_count": 0,
                "cost_usd": 0.0,
                "plan_file": None,
            },
            "task_graph": {},
            "agent_sessions": [],
            "cost_accumulator": {},
            "wal_position": 0,
        }
        bad_file.write_text(json.dumps(data), encoding="utf-8")
        # Should still load (JSON doesn't enforce types at rest)
        # but the data will have wrong runtime types.
        # This tests that we don't crash.
        result = load_checkpoint(bad_file)
        assert result is not None  # loads without crash


# ---------------------------------------------------------------------------
# list_checkpoints tests
# ---------------------------------------------------------------------------


class TestListCheckpoints:
    """Tests for list_checkpoints."""

    def test_list_empty_dir(self, tmp_path: Path) -> None:
        result = list_checkpoints(tmp_path)
        assert result == []

    def test_list_nonexistent_dir(self, tmp_path: Path) -> None:
        result = list_checkpoints(tmp_path / "nope")
        assert result == []

    def test_list_multiple_sorted(self, tmp_path: Path) -> None:
        # Create three checkpoints with different timestamps.
        for i, ts in enumerate(
            ["2026-04-10T14:00:00+00:00", "2026-04-10T12:00:00+00:00", "2026-04-10T16:00:00+00:00"]
        ):
            meta = CheckpointMetadata(
                checkpoint_id=f"ckpt-{i:03d}",
                created_at=ts,
                bernstein_version="1.5.0",
                task_count=10,
                completed_count=i,
                failed_count=0,
                cost_usd=0.0,
                plan_file=None,
            )
            ckpt = create_checkpoint(
                metadata=meta,
                task_graph={},
                agent_sessions=[],
                cost_accumulator={},
                wal_position=i,
            )
            save_checkpoint(ckpt, tmp_path)

        result = list_checkpoints(tmp_path)
        assert len(result) == 3
        # Should be sorted by created_at ascending
        assert result[0].created_at == "2026-04-10T12:00:00+00:00"
        assert result[1].created_at == "2026-04-10T14:00:00+00:00"
        assert result[2].created_at == "2026-04-10T16:00:00+00:00"

    def test_list_skips_corrupt_files(self, tmp_path: Path) -> None:
        # One valid checkpoint
        ckpt = _make_checkpoint()
        save_checkpoint(ckpt, tmp_path)

        # One corrupt file
        corrupt = tmp_path / "checkpoint-corrupt.json"
        corrupt.write_text("not json at all", encoding="utf-8")

        result = list_checkpoints(tmp_path)
        assert len(result) == 1
        assert result[0].checkpoint_id == "ckpt-001"

    def test_list_ignores_non_checkpoint_files(self, tmp_path: Path) -> None:
        ckpt = _make_checkpoint()
        save_checkpoint(ckpt, tmp_path)

        # Non-matching filename
        other = tmp_path / "something-else.json"
        other.write_text("{}", encoding="utf-8")

        result = list_checkpoints(tmp_path)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# validate_checkpoint tests
# ---------------------------------------------------------------------------


class TestValidateCheckpoint:
    """Tests for validate_checkpoint."""

    def test_valid_checkpoint(self) -> None:
        ckpt = _make_checkpoint()
        errors = validate_checkpoint(ckpt)
        assert errors == []

    def test_empty_checkpoint_id(self) -> None:
        meta = _make_metadata(checkpoint_id="")
        ckpt = create_checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=0,
        )
        errors = validate_checkpoint(ckpt)
        assert any("checkpoint_id is empty" in e for e in errors)

    def test_invalid_iso_timestamp(self) -> None:
        meta = CheckpointMetadata(
            checkpoint_id="ckpt-bad-ts",
            created_at="not-a-timestamp",
            bernstein_version="1.5.0",
            task_count=0,
            completed_count=0,
            failed_count=0,
            cost_usd=0.0,
            plan_file=None,
        )
        ckpt = create_checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=0,
        )
        errors = validate_checkpoint(ckpt)
        assert any("ISO-8601" in e for e in errors)

    def test_negative_counts(self) -> None:
        meta = _make_metadata(task_count=-1, completed_count=-2, failed_count=-3)
        ckpt = create_checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=0,
        )
        errors = validate_checkpoint(ckpt)
        assert any("task_count is negative" in e for e in errors)
        assert any("completed_count is negative" in e for e in errors)
        assert any("failed_count is negative" in e for e in errors)

    def test_counts_exceed_total(self) -> None:
        meta = _make_metadata(task_count=5, completed_count=4, failed_count=3)
        ckpt = create_checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=0,
        )
        errors = validate_checkpoint(ckpt)
        assert any("exceeds task_count" in e for e in errors)

    def test_negative_cost(self) -> None:
        meta = _make_metadata(cost_usd=-5.0)
        ckpt = create_checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=0,
        )
        errors = validate_checkpoint(ckpt)
        assert any("cost_usd is negative" in e for e in errors)

    def test_negative_wal_position(self) -> None:
        meta = _make_metadata()
        ckpt = Checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=-1,
        )
        errors = validate_checkpoint(ckpt)
        assert any("wal_position is negative" in e for e in errors)

    def test_multiple_errors(self) -> None:
        meta = CheckpointMetadata(
            checkpoint_id="",
            created_at="bad",
            bernstein_version="",
            task_count=-1,
            completed_count=-1,
            failed_count=-1,
            cost_usd=-1.0,
            plan_file=None,
        )
        ckpt = Checkpoint(
            metadata=meta,
            task_graph={},
            agent_sessions=[],
            cost_accumulator={},
            wal_position=-1,
        )
        errors = validate_checkpoint(ckpt)
        # Should report many errors at once
        assert len(errors) >= 5
