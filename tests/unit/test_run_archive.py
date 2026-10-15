"""Tests for CLI-020: full run archive export as ZIP."""

from __future__ import annotations

import hashlib
import json
import zipfile
from contextlib import suppress
from pathlib import Path, PureWindowsPath

import pytest

import bernstein
from bernstein.cli.run_archive import (
    ARCHIVE_ANCHOR_EVENT,
    ARCHIVE_SECTIONS,
    ArchiveManifest,
    ArchiveManifestError,
    collect_archive_files,
    create_archive,
    format_archive_summary,
    format_verification,
    manifest_digest,
    read_archive_manifest,
    verify_archive,
)
from bernstein.core.security.audit import AuditLog

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
    target = (Path(".sdd") / "metrics" / "perf.jsonl").as_posix()

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
    target = (Path(".sdd") / "audit" / "events.jsonl").as_posix()

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


class _WindowsRelativePath(type(Path())):  # type: ignore[misc]
    """A real file whose ``relative_to`` answers the way it does on Windows.

    ``str()`` of the result renders ``\\``, which is what ``str(f.relative_to(base))``
    produced on the Windows lane. Lets a Linux run see the Windows naming.
    """

    def relative_to(self, *other: object, **kwargs: object) -> PureWindowsPath:  # type: ignore[override]
        return PureWindowsPath(*super().relative_to(*other, **kwargs).parts)  # type: ignore[arg-type]


def test_member_names_are_posix_whichever_host_writes_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same tree must produce the same member names and manifest on any host.

    ``str(Path)`` renders ``\\`` on Windows, so an archive written there named
    its members ``.sdd\\metrics\\perf.jsonl`` and keyed the manifest the same way.
    Self-consistent, which is why every same-host test was green, and not what
    any other host or third-party zip tool expects.
    """
    import bernstein.cli.run_archive as run_archive

    _populate_sdd(tmp_path)
    real = run_archive.collect_archive_files

    def windows_files(base_dir: Path, sections: list[str] | None = None) -> list[Path]:
        return [_WindowsRelativePath(f) for f in real(base_dir, sections)]

    monkeypatch.setattr(run_archive, "collect_archive_files", windows_files)
    archive = tmp_path / "run.zip"
    manifest = create_archive(tmp_path, archive)

    with zipfile.ZipFile(archive) as zf:
        members = {name for name in zf.namelist() if name != "manifest.json"}
    nested = ".sdd/metrics/perf.jsonl"
    assert nested in members
    assert nested in manifest.files
    assert not any("\\" in name for name in members | set(manifest.files))
    assert verify_archive(archive).ok is True


def test_a_manifest_missing_a_required_field_is_refused_not_a_traceback(tmp_path: Path) -> None:
    """Parses, is an object, lacks ``file_count``: the third way a manifest fails to mean something."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    payload = json.loads(zipfile.ZipFile(archive).read("manifest.json"))
    del payload["file_count"]
    truncated = tmp_path / "truncated-manifest.zip"
    _rewrite_member(archive, truncated, "manifest.json", json.dumps(payload).encode("utf-8"))

    with pytest.raises(ArchiveManifestError, match="incomplete manifest.json"):
        read_archive_manifest(truncated)
    with pytest.raises(ArchiveManifestError, match="incomplete manifest.json"):
        verify_archive(truncated)


def test_a_manifest_that_contradicts_itself_is_unverifiable(tmp_path: Path) -> None:
    """``file_count`` and ``files`` are written from one list, so they must agree."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    payload = json.loads(zipfile.ZipFile(archive).read("manifest.json"))
    payload["file_count"] += 1
    edited = tmp_path / "edited-manifest.zip"
    _rewrite_member(archive, edited, "manifest.json", json.dumps(payload).encode("utf-8"))

    result = verify_archive(edited)
    assert result.ok is False
    assert result.unverifiable is not None
    assert "contradicts itself" in result.unverifiable


def test_a_member_that_fails_its_crc_is_modified_not_an_unreadable_archive(tmp_path: Path) -> None:
    """Bytes changed in place without rewriting the header trip zipfile's CRC check first."""
    _populate_sdd(tmp_path)
    archive = tmp_path / "run.zip"
    create_archive(tmp_path, archive)
    target = ".sdd/metrics/perf.jsonl"
    # Rewritten uncompressed, so a byte of the member's data sits at a known offset.
    stored = tmp_path / "stored.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(stored, "w") as out:
        for member in source.infolist():
            out.writestr(member.filename, source.read(member.filename))
    data = bytearray(stored.read_bytes())
    with zipfile.ZipFile(stored) as zf:
        offset = zf.getinfo(target).header_offset
    # 30-byte local header, then the name; writestr adds no extra field.
    data[offset + 30 + len(target.encode())] ^= 0x01
    stored.write_bytes(bytes(data))

    result = verify_archive(stored)
    assert result.ok is False
    assert result.modified == [target]


# ---------------------------------------------------------------------------
# Manifest anchor (#6258): the hashes detect modification, the anchor speaks
# to authorship, and the two are reported apart.
# ---------------------------------------------------------------------------


def _audit_log(where: Path) -> AuditLog:
    return AuditLog(where, key=b"archive-anchor-test-key-32-bytes")


def _forge(archive: Path, member: str, body: bytes) -> None:
    """Edit a member AND rewrite the manifest to match, as someone with write access could."""
    with zipfile.ZipFile(archive) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members["manifest.json"])
    members[member] = body
    manifest["files"][member] = hashlib.sha256(body).hexdigest()
    members["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    archive.unlink()
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_an_anchored_archive_verifies_and_is_attested(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    log = _audit_log(tmp_path / "operator-audit")
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=log)

    result = verify_archive(archive, audit_log=log)

    assert result.ok
    assert result.attestation == "anchored"
    records = log.query(event_type=ARCHIVE_ANCHOR_EVENT)
    assert [r.resource_id for r in records] == [manifest_digest(archive)]
    assert records[0].details["run_id"] == "run-42"


def test_a_forged_manifest_passes_the_hashes_and_fails_the_anchor(tmp_path: Path) -> None:
    """The case the anchor exists for: edit a member, recompute its hash, rewrite the manifest."""
    _populate_sdd(tmp_path)
    log = _audit_log(tmp_path / "operator-audit")
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=log)

    _forge(archive, ".sdd/tasks/tasks.jsonl", b'{"rewritten": true}\n')
    result = verify_archive(archive, audit_log=log)

    assert result.ok, "the forged manifest is self-consistent, so the hash check alone passes"
    assert result.attestation == "unattested"
    assert result.attestation_detail is not None
    assert manifest_digest(archive) in result.attestation_detail


def test_without_an_audit_log_attestation_is_unchecked_not_unattested(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive)

    result = verify_archive(archive)

    assert result.ok
    assert result.attestation == "unchecked"
    assert format_verification(archive, result) == f"OK  {archive}"


def test_an_archive_never_anchored_is_unattested_against_a_log(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    log = _audit_log(tmp_path / "operator-audit")
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive)

    result = verify_archive(archive, audit_log=log)

    assert result.ok
    assert result.attestation == "unattested"


def test_a_record_in_a_chain_that_does_not_verify_vouches_for_nothing(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    audit_dir = tmp_path / "operator-audit"
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=_audit_log(audit_dir))
    (day_file,) = audit_dir.glob("*.jsonl")
    day_file.write_text(day_file.read_text(encoding="utf-8").replace("run-42", "run-43"), encoding="utf-8")

    result = verify_archive(archive, audit_log=_audit_log(audit_dir))

    assert result.attestation == "unattested"
    assert result.attestation_detail is not None
    assert "does not verify" in result.attestation_detail


def test_the_report_names_the_attestation_beside_the_hash_result(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    log = _audit_log(tmp_path / "operator-audit")
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=log)
    _forge(archive, ".sdd/tasks/tasks.jsonl", b"{}\n{}\n")

    report = format_verification(archive, verify_archive(archive, audit_log=log))

    assert report.splitlines()[0] == f"OK  {archive}"
    assert "attestation: UNATTESTED" in report


def test_the_anchor_hashes_the_manifest_bytes_as_written(tmp_path: Path) -> None:
    _populate_sdd(tmp_path)
    log = _audit_log(tmp_path / "operator-audit")
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=log)
    with zipfile.ZipFile(archive) as zf:
        written = zf.read("manifest.json")

    assert manifest_digest(archive) == hashlib.sha256(written).hexdigest()


def test_a_record_appended_after_verification_never_anchors(tmp_path: Path) -> None:
    """The race from the #6502 review: a forged record lands after the chain was verified.

    Reading the anchor as `verify()` and then an unauthenticated `query()` counted a line appended
    between the two calls, so a forged member plus a forged manifest read `ok=True,
    attestation=anchored`. The anchor is now one authenticated read, which sees the forged line
    and refuses it.
    """
    _populate_sdd(tmp_path)
    audit_dir = tmp_path / "operator-audit"
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=_audit_log(audit_dir))
    _forge(archive, ".sdd/tasks/tasks.jsonl", b'{"rewritten": true}\n')
    (day_file,) = audit_dir.glob("*.jsonl")
    last = json.loads(day_file.read_text(encoding="utf-8").splitlines()[-1])
    forged = {**last, "resource_id": manifest_digest(archive), "hmac": "0" * 64}

    log = _audit_log(audit_dir)
    real_verify = log.verify

    def _verify_then_append() -> tuple[bool, list[str]]:
        verdict = real_verify()
        with day_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(forged) + "\n")
        return verdict

    log.verify = _verify_then_append  # type: ignore[method-assign]
    result = verify_archive(archive, audit_log=log)

    assert result.ok, "the forged manifest is self-consistent, so the hash check alone passes"
    assert result.attestation != "anchored"


def test_the_anchor_read_is_a_single_authenticated_scan(tmp_path: Path) -> None:
    """No unauthenticated `query` is consulted, so nothing can slip in between two reads."""
    _populate_sdd(tmp_path)
    log = _audit_log(tmp_path / "operator-audit")
    archive = tmp_path / "out.zip"
    create_archive(tmp_path, archive, audit_log=log)

    def _no_query(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("the anchor must not be read through the unauthenticated query()")

    log.query = _no_query  # type: ignore[method-assign]
    result = verify_archive(archive, audit_log=log)

    assert result.attestation == "anchored"
