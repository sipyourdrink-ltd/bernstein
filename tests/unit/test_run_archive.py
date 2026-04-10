"""Tests for CLI-020: full run archive export as ZIP."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import bernstein
from bernstein.cli.run_archive import (
    ARCHIVE_SECTIONS,
    ArchiveManifest,
    collect_archive_files,
    create_archive,
    format_archive_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_sdd(base: Path) -> None:
    """Create a minimal .sdd/ tree with one file per section."""
    (base / ".sdd" / "tasks").mkdir(parents=True)
    (base / ".sdd" / "tasks" / "tasks.jsonl").write_text("{}\n", encoding="utf-8")

    (base / ".sdd" / "runtime" / "costs").mkdir(parents=True)
    (base / ".sdd" / "runtime" / "orchestrator.log").write_text("log\n", encoding="utf-8")
    (base / ".sdd" / "runtime" / "costs" / "c1.json").write_text("{}\n", encoding="utf-8")
    (base / ".sdd" / "runtime" / "run_id").write_text("run-42\n", encoding="utf-8")

    (base / ".sdd" / "audit").mkdir(parents=True)
    (base / ".sdd" / "audit" / "events.jsonl").write_text("{}\n", encoding="utf-8")

    (base / ".sdd" / "metrics").mkdir(parents=True)
    (base / ".sdd" / "metrics" / "perf.jsonl").write_text("{}\n", encoding="utf-8")

    (base / ".sdd" / "traces").mkdir(parents=True)
    (base / ".sdd" / "traces" / "t1.json").write_text("{}\n", encoding="utf-8")

    (base / ".sdd" / "config").mkdir(parents=True)
    (base / ".sdd" / "config" / "settings.yaml").write_text("k: v\n", encoding="utf-8")

    (base / "bernstein.yaml").write_text("project: demo\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# ArchiveManifest creation
# ---------------------------------------------------------------------------


def test_archive_manifest_creation() -> None:
    m = ArchiveManifest(
        created_at="2026-04-10T00:00:00+00:00",
        bernstein_version="1.5.0",
        run_id="abc-123",
        file_count=10,
        total_size_bytes=2048,
        sections=["tasks", "logs"],
    )
    assert m.created_at == "2026-04-10T00:00:00+00:00"
    assert m.bernstein_version == "1.5.0"
    assert m.run_id == "abc-123"
    assert m.file_count == 10
    assert m.total_size_bytes == 2048
    assert m.sections == ["tasks", "logs"]


def test_archive_manifest_is_frozen() -> None:
    m = ArchiveManifest(
        created_at="t",
        bernstein_version="v",
        run_id=None,
        file_count=0,
        total_size_bytes=0,
    )
    try:
        m.file_count = 99  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# collect_archive_files
# ---------------------------------------------------------------------------


def test_collect_archive_files_all_sections(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    files = collect_archive_files(tmp_path)
    names = {f.name for f in files}
    assert "tasks.jsonl" in names
    assert "orchestrator.log" in names
    assert "c1.json" in names
    assert "events.jsonl" in names
    assert "perf.jsonl" in names
    assert "t1.json" in names
    assert "settings.yaml" in names
    assert "bernstein.yaml" in names


def test_collect_archive_files_section_filter(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    files = collect_archive_files(tmp_path, sections=["tasks", "audit"])
    names = {f.name for f in files}
    assert "tasks.jsonl" in names
    assert "events.jsonl" in names
    # Other sections excluded
    assert "orchestrator.log" not in names
    assert "c1.json" not in names
    assert "bernstein.yaml" not in names


def test_collect_archive_files_empty_directory(tmp_path: Path) -> None:
    # No .sdd/ at all — should return empty list without error
    files = collect_archive_files(tmp_path)
    assert files == []


def test_collect_archive_files_deduplicates(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    files = collect_archive_files(tmp_path)
    assert len(files) == len(set(files))


def test_collect_archive_files_sorted(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    files = collect_archive_files(tmp_path)
    assert files == sorted(files)


# ---------------------------------------------------------------------------
# create_archive
# ---------------------------------------------------------------------------


def test_create_archive_produces_valid_zip(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    out = tmp_path / "archive.zip"
    manifest = create_archive(tmp_path, out)

    assert out.exists()
    assert zipfile.is_zipfile(out)
    assert manifest.file_count > 0
    assert manifest.total_size_bytes > 0


def test_create_archive_includes_manifest_json(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    out = tmp_path / "archive.zip"
    manifest = create_archive(tmp_path, out)

    with zipfile.ZipFile(out) as zf:
        assert "manifest.json" in zf.namelist()
        data = json.loads(zf.read("manifest.json"))
        assert data["bernstein_version"] == bernstein.__version__
        assert data["file_count"] == manifest.file_count
        assert data["total_size_bytes"] == manifest.total_size_bytes


def test_create_archive_contains_all_files(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    out = tmp_path / "archive.zip"
    create_archive(tmp_path, out)

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        # Exclude manifest.json when checking data files
        data_names = [n for n in names if n != "manifest.json"]
        assert len(data_names) > 0
        # All entries should be relative paths (no leading /)
        for name in data_names:
            assert not name.startswith("/")


def test_create_archive_with_section_filter(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    out = tmp_path / "archive.zip"
    manifest = create_archive(tmp_path, out, sections=["tasks"])

    assert manifest.sections == ["tasks"]
    with zipfile.ZipFile(out) as zf:
        data_names = [n for n in zf.namelist() if n != "manifest.json"]
        assert all("tasks" in n for n in data_names)


def test_create_archive_reads_run_id(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    out = tmp_path / "archive.zip"
    manifest = create_archive(tmp_path, out)
    assert manifest.run_id == "run-42"


def test_create_archive_run_id_none_when_missing(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    (tmp_path / ".sdd" / "runtime" / "run_id").unlink()
    out = tmp_path / "archive.zip"
    manifest = create_archive(tmp_path, out)
    assert manifest.run_id is None


# ---------------------------------------------------------------------------
# format_archive_summary
# ---------------------------------------------------------------------------


def test_format_archive_summary_readable() -> None:
    m = ArchiveManifest(
        created_at="2026-04-10T12:00:00+00:00",
        bernstein_version="1.5.0",
        run_id="run-42",
        file_count=8,
        total_size_bytes=4096,
        sections=["audit", "config", "costs", "logs", "metrics", "tasks", "traces"],
    )
    text = format_archive_summary(m)
    assert "Archive Summary" in text
    assert "2026-04-10T12:00:00+00:00" in text
    assert "1.5.0" in text
    assert "run-42" in text
    assert "8" in text
    assert "4.0 KB" in text
    assert "tasks" in text
    assert "audit" in text


def test_format_archive_summary_no_run_id() -> None:
    m = ArchiveManifest(
        created_at="2026-04-10T00:00:00+00:00",
        bernstein_version="1.5.0",
        run_id=None,
        file_count=0,
        total_size_bytes=0,
    )
    text = format_archive_summary(m)
    assert "(none)" in text


def test_format_archive_summary_no_sections() -> None:
    m = ArchiveManifest(
        created_at="2026-04-10T00:00:00+00:00",
        bernstein_version="1.5.0",
        run_id=None,
        file_count=0,
        total_size_bytes=0,
        sections=[],
    )
    text = format_archive_summary(m)
    assert "Sections:  (none)" in text


# ---------------------------------------------------------------------------
# ARCHIVE_SECTIONS constant
# ---------------------------------------------------------------------------


def test_archive_sections_keys() -> None:
    expected = {"tasks", "logs", "costs", "audit", "metrics", "traces", "config"}
    assert set(ARCHIVE_SECTIONS.keys()) == expected
