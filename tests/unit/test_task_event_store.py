"""Tests for the append-only task event store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.task_event_store import (
    TaskEvent,
    TaskEventKind,
    TaskEventStore,
    record_transition,
)


class TestTaskEventKind:
    def test_all_values_are_strings(self) -> None:
        for member in TaskEventKind:
            assert isinstance(member.value, str)
            assert member.value == member.name

    def test_expected_members(self) -> None:
        names = {m.name for m in TaskEventKind}
        assert names == {
            "CREATED",
            "CLAIMED",
            "STARTED",
            "COMPLETED",
            "VERIFIED",
            "MERGED",
            "CLOSED",
            "FAILED",
            "BLOCKED",
            "UNBLOCKED",
            "CANCELLED",
        }


class TestTaskEvent:
    def test_frozen(self) -> None:
        evt = TaskEvent(
            task_id="t1",
            kind=TaskEventKind.CREATED,
            timestamp="2026-01-01T00:00:00+00:00",
            actor="orchestrator",
        )
        with pytest.raises(AttributeError):
            evt.task_id = "t2"  # type: ignore[misc]

    def test_to_dict(self) -> None:
        evt = TaskEvent(
            task_id="t1",
            kind=TaskEventKind.STARTED,
            timestamp="2026-01-01T00:00:00+00:00",
            actor="agent-42",
            metadata={"branch": "feat-x"},
        )
        d = evt.to_dict()
        assert d == {
            "task_id": "t1",
            "kind": "STARTED",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "actor": "agent-42",
            "metadata": {"branch": "feat-x"},
        }

    def test_from_dict_round_trip(self) -> None:
        original = TaskEvent(
            task_id="t1",
            kind=TaskEventKind.COMPLETED,
            timestamp="2026-06-15T12:30:00+00:00",
            actor="agent-7",
            metadata={"files_changed": 3},
        )
        restored = TaskEvent.from_dict(original.to_dict())
        assert restored == original

    def test_from_dict_missing_metadata_defaults_empty(self) -> None:
        data = {
            "task_id": "t1",
            "kind": "FAILED",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "actor": "orchestrator",
        }
        evt = TaskEvent.from_dict(data)
        assert evt.metadata == {}

    def test_default_metadata_is_empty_dict(self) -> None:
        evt = TaskEvent(
            task_id="t1",
            kind=TaskEventKind.CREATED,
            timestamp="2026-01-01T00:00:00+00:00",
            actor="orchestrator",
        )
        assert evt.metadata == {}


class TestTaskEventStore:
    def test_append_and_read_back(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        evt = TaskEvent(
            task_id="t1",
            kind=TaskEventKind.CREATED,
            timestamp="2026-01-01T00:00:00+00:00",
            actor="orchestrator",
        )
        store.append(evt)
        events = store.events_for("t1")
        assert len(events) == 1
        assert events[0] == evt

    def test_current_state_returns_latest(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        store.append(TaskEvent("t1", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        store.append(TaskEvent("t1", TaskEventKind.CLAIMED, "2026-01-01T00:01:00+00:00", "agent-1"))
        store.append(TaskEvent("t1", TaskEventKind.STARTED, "2026-01-01T00:02:00+00:00", "agent-1"))
        assert store.current_state("t1") == "STARTED"

    def test_events_for_chronological_order(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        kinds = [TaskEventKind.CREATED, TaskEventKind.CLAIMED, TaskEventKind.COMPLETED]
        for i, kind in enumerate(kinds):
            store.append(TaskEvent("t1", kind, f"2026-01-01T00:0{i}:00+00:00", "orch"))
        events = store.events_for("t1")
        assert [e.kind for e in events] == kinds

    def test_empty_task_returns_empty_list(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        assert store.events_for("nonexistent") == []

    def test_current_state_empty_returns_none(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        assert store.current_state("nonexistent") is None

    def test_multiple_tasks_do_not_interfere(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        store.append(TaskEvent("t1", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        store.append(TaskEvent("t2", TaskEventKind.FAILED, "2026-01-01T00:00:00+00:00", "orch"))
        assert store.current_state("t1") == "CREATED"
        assert store.current_state("t2") == "FAILED"
        assert len(store.events_for("t1")) == 1
        assert len(store.events_for("t2")) == 1

    def test_all_task_ids(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        store.append(TaskEvent("beta", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        store.append(TaskEvent("alpha", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        assert store.all_task_ids() == ["alpha", "beta"]

    def test_all_task_ids_empty_store(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        assert store.all_task_ids() == []

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "events"
        store = TaskEventStore(nested)
        store.append(TaskEvent("t1", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        assert nested.is_dir()
        assert len(store.events_for("t1")) == 1

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        store.append(TaskEvent("t1", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        # Inject a corrupt line
        task_file = tmp_path / "events" / "t1.jsonl"
        with task_file.open("a", encoding="utf-8") as fh:
            fh.write("NOT VALID JSON\n")
        store.append(TaskEvent("t1", TaskEventKind.STARTED, "2026-01-01T00:01:00+00:00", "agent-1"))
        events = store.events_for("t1")
        assert len(events) == 2
        assert events[0].kind == TaskEventKind.CREATED
        assert events[1].kind == TaskEventKind.STARTED

    def test_jsonl_file_format(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        store.append(TaskEvent("t1", TaskEventKind.CREATED, "2026-01-01T00:00:00+00:00", "orch"))
        raw = (tmp_path / "events" / "t1.jsonl").read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["kind"] == "CREATED"
        assert parsed["task_id"] == "t1"


class TestRecordTransition:
    def test_creates_and_persists_event(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        evt = record_transition(store, "t1", TaskEventKind.CREATED, "orchestrator")
        assert evt.task_id == "t1"
        assert evt.kind == TaskEventKind.CREATED
        assert evt.actor == "orchestrator"
        assert evt.timestamp  # non-empty ISO string
        # Event was persisted
        events = store.events_for("t1")
        assert len(events) == 1
        assert events[0] == evt

    def test_metadata_kwargs_passed_through(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        evt = record_transition(
            store,
            "t1",
            TaskEventKind.COMPLETED,
            "agent-5",
            files_changed=2,
            tests_passing=True,
        )
        assert evt.metadata == {"files_changed": 2, "tests_passing": True}

    def test_no_metadata_yields_empty_dict(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        evt = record_transition(store, "t1", TaskEventKind.BLOCKED, "orch")
        assert evt.metadata == {}

    def test_round_trip_via_record_transition(self, tmp_path: Path) -> None:
        store = TaskEventStore(tmp_path / "events")
        record_transition(store, "t1", TaskEventKind.CREATED, "orch")
        record_transition(store, "t1", TaskEventKind.CLAIMED, "agent-1")
        record_transition(store, "t1", TaskEventKind.STARTED, "agent-1")
        record_transition(store, "t1", TaskEventKind.COMPLETED, "agent-1", pr_url="https://gh/1")
        events = store.events_for("t1")
        assert len(events) == 4
        assert [e.kind for e in events] == [
            TaskEventKind.CREATED,
            TaskEventKind.CLAIMED,
            TaskEventKind.STARTED,
            TaskEventKind.COMPLETED,
        ]
        assert events[-1].metadata == {"pr_url": "https://gh/1"}
        assert store.current_state("t1") == "COMPLETED"
