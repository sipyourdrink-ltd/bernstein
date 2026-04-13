"""Tests for diagnostic delta capture and file-edit fallback replay artifact."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.traces import (
    EditReplayArtifact,
    EditReplayStore,
    FileEditDelta,
    capture_edit_delta,
    create_edit_replay_artifact,
)

# ---------------------------------------------------------------------------
# FileEditDelta — capture_edit_delta()
# ---------------------------------------------------------------------------


class TestCaptureEditDelta:
    def test_success_basic_edit(self) -> None:
        before = "def foo():\n    return 1\n"
        after = "def foo():\n    return 2\n"
        delta = capture_edit_delta("src/foo.py", before, after)

        assert delta.file_path == "src/foo.py"
        assert delta.before_content == before
        assert delta.after_content == after
        assert delta.lines_added == 1
        assert delta.lines_removed == 1
        assert delta.truncated is False
        assert "return 2" in delta.unified_diff
        assert "return 1" in delta.unified_diff

    def test_no_change_produces_empty_diff(self) -> None:
        content = "x = 1\n"
        delta = capture_edit_delta("f.py", content, content)

        assert delta.lines_added == 0
        assert delta.lines_removed == 0
        assert delta.unified_diff == ""

    def test_new_file_empty_before(self) -> None:
        delta = capture_edit_delta("new.py", "", "x = 1\n")

        assert delta.lines_added == 1
        assert delta.lines_removed == 0
        assert delta.before_content == ""

    def test_deleted_file_empty_after(self) -> None:
        delta = capture_edit_delta("gone.py", "x = 1\n", "")

        assert delta.lines_added == 0
        assert delta.lines_removed == 1
        assert delta.after_content == ""

    def test_session_and_task_ids_stored(self) -> None:
        delta = capture_edit_delta("f.py", "a\n", "b\n", session_id="sess-1", task_id="task-abc")

        assert delta.session_id == "sess-1"
        assert delta.task_id == "task-abc"

    def test_truncation_when_content_exceeds_max_bytes(self) -> None:
        large = "x" * 200
        delta = capture_edit_delta("big.py", large, large + "y", max_bytes=100)

        assert delta.truncated is True
        assert len(delta.before_content) <= 100
        assert len(delta.after_content) <= 100

    def test_no_truncation_for_small_content(self) -> None:
        delta = capture_edit_delta("small.py", "a\n", "b\n", max_bytes=100)

        assert delta.truncated is False

    def test_timestamp_is_positive(self) -> None:
        delta = capture_edit_delta("f.py", "", "hello\n")
        assert delta.timestamp > 0

    def test_multiline_add_counts_correctly(self) -> None:
        before = "line1\n"
        after = "line1\nline2\nline3\n"
        delta = capture_edit_delta("f.py", before, after)

        assert delta.lines_added == 2
        assert delta.lines_removed == 0


# ---------------------------------------------------------------------------
# FileEditDelta — serialisation round-trip
# ---------------------------------------------------------------------------


class TestFileEditDeltaSerialization:
    def test_round_trip_full(self) -> None:
        original = capture_edit_delta(
            "src/bar.py",
            "old\n",
            "new\n",
            session_id="s1",
            task_id="t1",
        )
        restored = FileEditDelta.from_dict(original.to_dict())

        assert restored.file_path == original.file_path
        assert restored.before_content == original.before_content
        assert restored.after_content == original.after_content
        assert restored.unified_diff == original.unified_diff
        assert restored.lines_added == original.lines_added
        assert restored.lines_removed == original.lines_removed
        assert restored.session_id == original.session_id
        assert restored.task_id == original.task_id
        assert restored.truncated == original.truncated

    def test_from_dict_missing_optional_fields(self) -> None:
        # Only required fields present
        d: dict[str, object] = {
            "file_path": "x.py",
            "before_content": "",
            "after_content": "",
            "unified_diff": "",
            "lines_added": 0,
            "lines_removed": 0,
            "timestamp": 1.0,
        }
        delta = FileEditDelta.from_dict(d)
        assert delta.session_id == ""
        assert delta.task_id == ""
        assert delta.truncated is False

    def test_to_dict_is_plain_dict(self) -> None:
        delta = capture_edit_delta("f.py", "a\n", "b\n")
        d = delta.to_dict()
        assert isinstance(d, dict)
        assert d["file_path"] == "f.py"


# ---------------------------------------------------------------------------
# EditReplayArtifact — create_edit_replay_artifact()
# ---------------------------------------------------------------------------


class TestCreateEditReplayArtifact:
    def test_success_case_fields_set(self) -> None:
        artifact = create_edit_replay_artifact(
            file_path="src/models.py",
            pre_edit_content="class Foo:\n    pass\n",
            edit_intent="add bar method to Foo",
            failure_reason="patch_mismatch",
            session_id="sess-1",
            task_id="task-xyz",
        )

        assert artifact.file_path == "src/models.py"
        assert "class Foo" in artifact.pre_edit_content
        assert artifact.edit_intent == "add bar method to Foo"
        assert artifact.failure_reason == "patch_mismatch"
        assert artifact.session_id == "sess-1"
        assert artifact.task_id == "task-xyz"
        assert artifact.truncated is False
        assert len(artifact.artifact_id) == 16

    def test_artifact_id_is_unique(self) -> None:
        a1 = create_edit_replay_artifact("f.py", "x\n", "intent", "reason")
        a2 = create_edit_replay_artifact("f.py", "x\n", "intent", "reason")
        assert a1.artifact_id != a2.artifact_id

    def test_truncation_when_content_exceeds_max_bytes(self) -> None:
        large_content = "y" * 200
        artifact = create_edit_replay_artifact(
            "huge.py",
            large_content,
            "add feature",
            "write_error",
            max_bytes=50,
        )

        assert artifact.truncated is True
        assert len(artifact.pre_edit_content) <= 50

    def test_no_truncation_for_small_content(self) -> None:
        artifact = create_edit_replay_artifact("f.py", "small\n", "fix", "mismatch", max_bytes=100)
        assert artifact.truncated is False

    def test_timestamp_is_positive(self) -> None:
        artifact = create_edit_replay_artifact("f.py", "", "intent", "reason")
        assert artifact.timestamp > 0


# ---------------------------------------------------------------------------
# EditReplayArtifact — serialisation round-trip
# ---------------------------------------------------------------------------


class TestEditReplayArtifactSerialization:
    def test_round_trip_full(self) -> None:
        original = create_edit_replay_artifact(
            "src/worker.py",
            "original content\n",
            "refactor worker loop",
            "low_confidence",
            session_id="s2",
            task_id="t2",
        )
        restored = EditReplayArtifact.from_dict(original.to_dict())

        assert restored.artifact_id == original.artifact_id
        assert restored.file_path == original.file_path
        assert restored.pre_edit_content == original.pre_edit_content
        assert restored.edit_intent == original.edit_intent
        assert restored.failure_reason == original.failure_reason
        assert restored.session_id == original.session_id
        assert restored.task_id == original.task_id
        assert restored.truncated == original.truncated

    def test_from_dict_missing_optional_fields(self) -> None:
        d: dict[str, object] = {
            "artifact_id": "abc123",
            "file_path": "x.py",
            "pre_edit_content": "",
            "edit_intent": "",
            "failure_reason": "",
            "timestamp": 2.0,
        }
        artifact = EditReplayArtifact.from_dict(d)
        assert artifact.session_id == ""
        assert artifact.task_id == ""
        assert artifact.truncated is False

    def test_to_dict_is_plain_dict(self) -> None:
        artifact = create_edit_replay_artifact("f.py", "x\n", "i", "r")
        d = artifact.to_dict()
        assert isinstance(d, dict)
        assert d["file_path"] == "f.py"
        assert "artifact_id" in d


# ---------------------------------------------------------------------------
# EditReplayStore — persistence
# ---------------------------------------------------------------------------


class TestEditReplayStore:
    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "artifacts")
        artifact = create_edit_replay_artifact(
            "src/core.py",
            "before\n",
            "add logging",
            "write_error",
            task_id="task-1",
        )
        store.write(artifact)

        loaded = store.read(artifact.artifact_id)
        assert loaded is not None
        assert loaded.artifact_id == artifact.artifact_id
        assert loaded.file_path == "src/core.py"
        assert loaded.pre_edit_content == "before\n"
        assert loaded.task_id == "task-1"

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "artifacts")
        assert store.read("deadbeef12345678") is None

    def test_read_missing_dir_returns_none(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "nonexistent" / "artifacts")
        assert store.read("any-id") is None

    def test_list_for_task_filters_by_task_id(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "artifacts")
        a1 = create_edit_replay_artifact("f1.py", "x\n", "i", "r", task_id="task-A")
        a2 = create_edit_replay_artifact("f2.py", "y\n", "i", "r", task_id="task-B")
        a3 = create_edit_replay_artifact("f3.py", "z\n", "i", "r", task_id="task-A")
        store.write(a1)
        store.write(a2)
        store.write(a3)

        results = store.list_for_task("task-A")
        ids = {a.artifact_id for a in results}
        assert a1.artifact_id in ids
        assert a3.artifact_id in ids
        assert a2.artifact_id not in ids

    def test_list_for_task_empty_when_no_match(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "artifacts")
        a = create_edit_replay_artifact("f.py", "x\n", "i", "r", task_id="task-A")
        store.write(a)

        assert store.list_for_task("task-nonexistent") == []

    def test_list_for_task_sorted_by_timestamp(self, tmp_path: Path) -> None:
        import time as _time

        store = EditReplayStore(tmp_path / "artifacts")
        a1 = create_edit_replay_artifact("f1.py", "x\n", "i", "r", task_id="task-T")
        _time.sleep(0.01)
        a2 = create_edit_replay_artifact("f2.py", "y\n", "i", "r", task_id="task-T")
        store.write(a1)
        store.write(a2)

        results = store.list_for_task("task-T")
        assert results[0].artifact_id == a1.artifact_id
        assert results[1].artifact_id == a2.artifact_id

    def test_write_overwrites_same_artifact_id(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "artifacts")
        artifact = create_edit_replay_artifact("f.py", "v1\n", "intent", "reason", task_id="t1")
        store.write(artifact)

        # Manually mutate a copy with same ID and re-write
        updated_dict = artifact.to_dict()
        updated_dict["pre_edit_content"] = "v2\n"
        updated = EditReplayArtifact.from_dict(updated_dict)
        store.write(updated)

        loaded = store.read(artifact.artifact_id)
        assert loaded is not None
        assert loaded.pre_edit_content == "v2\n"

    def test_list_for_task_empty_dir(self, tmp_path: Path) -> None:
        store = EditReplayStore(tmp_path / "no_artifacts_yet")
        assert store.list_for_task("any-task") == []

    def test_corrupt_artifact_file_is_skipped(self, tmp_path: Path) -> None:
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "corrupt.json").write_text("not valid json{{")

        store = EditReplayStore(artifacts_dir)
        a = create_edit_replay_artifact("f.py", "x\n", "i", "r", task_id="task-X")
        store.write(a)

        results = store.list_for_task("task-X")
        assert len(results) == 1
        assert results[0].artifact_id == a.artifact_id


# ---------------------------------------------------------------------------
# Integration: capture_edit_delta → create_edit_replay_artifact pipeline
# ---------------------------------------------------------------------------


class TestEditDeltaToReplayPipeline:
    """Verify the two-step diagnostic flow: capture delta, quarantine as replay artifact."""

    def test_delta_content_flows_into_replay_artifact(self) -> None:
        before = "def compute(x):\n    return x * 2\n"
        after = "def compute(x):\n    return x * 3\n"

        delta = capture_edit_delta("src/math.py", before, after, session_id="s1", task_id="t1")

        # Simulate: patch confidence was low, so quarantine as replay artifact
        artifact = create_edit_replay_artifact(
            file_path=delta.file_path,
            pre_edit_content=delta.before_content,
            edit_intent=delta.unified_diff[:200],
            failure_reason="low_confidence",
            session_id=delta.session_id,
            task_id=delta.task_id,
        )

        assert artifact.file_path == "src/math.py"
        assert "x * 2" in artifact.pre_edit_content  # original content preserved
        assert artifact.failure_reason == "low_confidence"
        assert artifact.task_id == "t1"

    def test_mismatch_delta_produces_non_empty_diff(self) -> None:
        """A failed/partial edit still yields a non-empty diff for audit purposes."""
        before = "a = 1\nb = 2\n"
        # Simulated partial write — only first line updated
        after = "a = 99\nb = 2\n"

        delta = capture_edit_delta("partial.py", before, after)
        assert delta.lines_added >= 1
        assert delta.lines_removed >= 1
        assert len(delta.unified_diff) > 0

    @pytest.mark.parametrize(
        "failure_reason",
        ["patch_mismatch", "write_error", "low_confidence", "timeout"],
    )
    def test_all_failure_reasons_stored(self, failure_reason: str) -> None:
        artifact = create_edit_replay_artifact("f.py", "content\n", "intent", failure_reason)
        assert artifact.failure_reason == failure_reason
        restored = EditReplayArtifact.from_dict(artifact.to_dict())
        assert restored.failure_reason == failure_reason
