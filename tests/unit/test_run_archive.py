"""Tests for CLI-020: full run archive export as ZIP."""

from __future__ import annotations

import json
import zipfile
from contextlib import suppress
from pathlib import Path

import pytest

import bernstein
from bernstein.cli.run_archive import (
    ARCHIVE_SECTIONS,
    ArchiveManifest,
    ArchiveManifestError,
    collect_archive_files,
    create_archive,
    format_archive_summary,
    format_verification,
    read_archive_manifest,
    verify_archive,
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
    with suppress(AttributeError):
        m.file_count = 99  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover


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
    # No .sdd/ at all - should return empty list without error
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


# ---------------------------------------------------------------------------
# Per-file hashes and offline verification (#5443)
# ---------------------------------------------------------------------------
#
# An unattended run on an ephemeral workspace deletes the workspace when it
# ends, so the archive IS the evidence. The manifest recorded a file count and
# a byte total, which say how much was collected and nothing about what -- an
# archive that lost a file, or had one edited afterwards, read as intact.


def _rewrite_member(src: Path, dst: Path, name: str, data: bytes | None) -> None:
    """Copy an archive, replacing one member's bytes, or dropping it entirely.

    Rewritten rather than edited in place: a zip is not a byte stream you can
    patch, and the point is to produce an archive that LOOKS well-formed.
    """
    with zipfile.ZipFile(src) as source, zipfile.ZipFile(dst, "w") as out:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == name:
                if data is None:
                    continue
                payload = data
            out.writestr(info, payload)


def test_the_manifest_lists_every_archived_file_with_a_hash(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    manifest = create_archive(tmp_path, archive)

    with zipfile.ZipFile(archive) as zf:
        members = {name for name in zf.namelist() if name != "manifest.json"}

    assert set(manifest.files) == members, "the manifest must describe exactly what the archive holds"
    assert manifest.file_count == len(manifest.files)
    assert all(len(digest) == 64 for digest in manifest.files.values())
    # And it travels inside the archive, so a reader holding only the file can
    # check it without the workspace, which by then is usually gone.
    assert read_archive_manifest(archive).files == manifest.files


def test_an_untouched_archive_verifies(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)

    result = verify_archive(archive)
    assert result.ok is True
    assert (result.modified, result.missing, result.unexpected) == ([], [], [])
    assert format_verification(archive, result).startswith("OK")


def test_a_modified_byte_fails_verification(tmp_path: Path) -> None:
    """The acceptance criterion, and the reason the hashes exist at all."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    target = str(Path(".sdd") / "metrics" / "perf.jsonl")

    with zipfile.ZipFile(archive) as zf:
        original = zf.read(target)
    tampered = tmp_path / "tampered.zip"
    _rewrite_member(archive, tampered, target, original.replace(b"{}", b"{ }", 1))

    result = verify_archive(tampered)
    assert result.ok is False
    assert result.modified == [target]
    assert "modified since the archive was written" in format_verification(tampered, result)


def test_a_file_that_did_not_survive_is_reported_as_missing_not_modified(tmp_path: Path) -> None:
    """A different fact, and a different thing to do about it.

    Evidence that changed after the fact and evidence that never arrived are
    not the same finding, so collapsing both into `ok: False` would leave the
    operator to guess which happened.
    """
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    target = str(Path(".sdd") / "audit" / "events.jsonl")

    truncated = tmp_path / "truncated.zip"
    _rewrite_member(archive, truncated, target, None)

    result = verify_archive(truncated)
    assert result.ok is False
    assert result.missing == [target]
    assert result.modified == []


def test_a_member_nothing_vouches_for_is_reported(tmp_path: Path) -> None:
    """Added after the fact, so no hash covers it."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)

    padded = tmp_path / "padded.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(padded, "w") as out:
        for info in source.infolist():
            out.writestr(info, source.read(info.filename))
        out.writestr(".sdd/metrics/planted.jsonl", '{"planted": true}\n')

    result = verify_archive(padded)
    assert result.ok is False
    assert result.unexpected == [".sdd/metrics/planted.jsonl"]


def test_an_archive_without_hashes_is_unverifiable_rather_than_verified(tmp_path: Path) -> None:
    """An archive written before this existed must not report OK.

    Its manifest has no `files`, so nothing is checked -- and "nothing failed"
    is not the same claim as "everything matched". Reporting OK here would be
    the exact false assurance the hashes were added to remove.
    """
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)

    legacy_manifest = json.loads(zipfile.ZipFile(archive).read("manifest.json"))
    del legacy_manifest["files"]
    legacy = tmp_path / "legacy.zip"
    _rewrite_member(archive, legacy, "manifest.json", json.dumps(legacy_manifest).encode("utf-8"))

    result = verify_archive(legacy)
    assert result.ok is False
    assert result.unverifiable is not None
    assert "no per-file hashes" in result.unverifiable
    assert format_verification(legacy, result).startswith("UNVERIFIABLE")


def test_an_archive_with_no_manifest_is_refused(tmp_path: Path) -> None:
    """Not an empty manifest: that would verify clean, having checked nothing."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    stripped = tmp_path / "stripped.zip"
    _rewrite_member(archive, stripped, "manifest.json", None)

    with pytest.raises(ArchiveManifestError, match="no manifest.json"):
        verify_archive(stripped)


def test_a_manifest_from_an_older_bernstein_still_reads(tmp_path: Path) -> None:
    """Unknown keys are dropped rather than raising, so an older or newer
    archive can still be inspected by whichever version is holding it."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    payload = json.loads(zipfile.ZipFile(archive).read("manifest.json"))
    payload["a_field_from_the_future"] = 1
    forward = tmp_path / "forward.zip"
    _rewrite_member(archive, forward, "manifest.json", json.dumps(payload).encode("utf-8"))

    assert (
        read_archive_manifest(forward).run_id
        == ArchiveManifest(**{k: v for k, v in payload.items() if k != "a_field_from_the_future"}).run_id
    )
