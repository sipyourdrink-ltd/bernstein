"""Tests for trace utilities — T566, T560, T585."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.traces import (
    build_crash_bundle,
    preview_edit_conflict,
    score_patch_match,
)


class TestScorePatchMatch:
    def test_identical_content_returns_confidence_one(self) -> None:
        result = score_patch_match("hello world", "hello world", "test.py")
        assert result.confidence == 1.0
        assert result.matched is True
        assert result.diff_lines == 0

    def test_small_change_returns_high_confidence(self) -> None:
        original = "def foo():\n    return 1\n"
        patched = "def foo():\n    return 2\n"
        result = score_patch_match(original, patched, "foo.py")
        assert result.confidence >= 0.5
        assert result.matched is True
        assert result.diff_lines >= 1

    def test_completely_different_content_returns_low_confidence(self) -> None:
        original = "x = 1\n" * 20
        patched = "y = 2\n" * 20
        result = score_patch_match(original, patched, "bar.py")
        assert result.confidence < 0.5
        assert result.matched is False
        assert "Low similarity" in result.mismatch_reason

    def test_file_path_is_preserved(self) -> None:
        result = score_patch_match("a", "b", "my/file.py")
        assert result.file_path == "my/file.py"

    def test_snippets_are_truncated_to_200_chars(self) -> None:
        long_content = "x" * 500
        result = score_patch_match(long_content, long_content + "y", "f.py")
        assert len(result.before_snippet) <= 200
        assert len(result.after_snippet) <= 200


class TestPreviewEditConflict:
    def test_identical_content_no_conflict(self) -> None:
        result = preview_edit_conflict("f.py", "same\n", "same\n", "s1", "s2")
        assert "no conflict" in result.resolution_hint
        assert result.conflict_lines == []

    def test_different_content_produces_conflict(self) -> None:
        result = preview_edit_conflict("f.py", "line1\nline2\n", "line1\nlineX\n", "s1", "s2")
        assert len(result.conflict_lines) >= 1
        assert result.session_a == "s1"
        assert result.session_b == "s2"

    def test_single_region_conflict_hint(self) -> None:
        result = preview_edit_conflict("f.py", "a\nb\nc\n", "a\nB\nc\n", "s1", "s2")
        # Single region conflict
        assert "conflict" in result.resolution_hint.lower() or "no conflict" in result.resolution_hint

    def test_snippets_are_populated(self) -> None:
        result = preview_edit_conflict("f.py", "content_a\n", "content_b\n", "s1", "s2")
        assert "content_a" in result.snippet_a
        assert "content_b" in result.snippet_b

    def test_to_dict_roundtrip(self) -> None:
        result = preview_edit_conflict("f.py", "a\n", "b\n", "s1", "s2")
        d = result.to_dict()
        assert d["file_path"] == "f.py"
        assert d["session_a"] == "s1"
        assert "conflict_lines" in d


class TestBuildCrashBundle:
    def test_bundle_has_required_keys(self, tmp_path: Path) -> None:
        bundle = build_crash_bundle(tmp_path)
        assert "captured_at" in bundle
        assert "workdir" in bundle
        assert "traces" in bundle
        assert "metrics_summary" in bundle
        assert "runtime_files" in bundle

    def test_bundle_with_trace_files(self, tmp_path: Path) -> None:
        traces_dir = tmp_path / ".sdd" / "traces"
        traces_dir.mkdir(parents=True)
        (traces_dir / "trace1.jsonl").write_text('{"session_id": "s1"}\n')
        bundle = build_crash_bundle(tmp_path)
        assert len(bundle["traces"]) == 1
        assert bundle["traces"][0]["file"] == "trace1.jsonl"

    def test_bundle_with_metrics(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        (metrics_dir / "metrics.jsonl").write_text("{}\n")
        bundle = build_crash_bundle(tmp_path)
        assert bundle["metrics_summary"]["file_count"] == 1

    def test_bundle_respects_max_trace_bytes(self, tmp_path: Path) -> None:
        traces_dir = tmp_path / ".sdd" / "traces"
        traces_dir.mkdir(parents=True)
        (traces_dir / "big.jsonl").write_text("x" * 10_000)
        bundle = build_crash_bundle(tmp_path, max_trace_bytes=100)
        # Content should be truncated
        total = sum(len(t["content"]) for t in bundle["traces"])
        assert total <= 100
