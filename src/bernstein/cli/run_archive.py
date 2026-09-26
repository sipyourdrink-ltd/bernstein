"""Export full run archive as ZIP.

Collects all run state from ``.sdd/`` (tasks, logs, costs, audit, metrics,
traces, config) plus the top-level ``bernstein.yaml`` and packages them into
a single ZIP archive with an embedded ``manifest.json``.
"""

from __future__ import annotations

import glob as _glob
import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

import bernstein
from bernstein.cli.run_names import render_name

#: Read size for hashing. Large enough that a multi-megabyte trace store does
#: not become a million small reads, small enough not to hold one in memory.
_HASH_CHUNK_BYTES = 1024 * 1024


class ArchiveManifestError(RuntimeError):
    """An archive has no manifest, or one that cannot be read."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveManifest:
    """Metadata embedded in the archive as ``manifest.json``.

    ``files`` is the retention contract in machine-readable form: one entry per
    archived member, with its sha256. A count and a byte total say how much was
    collected and nothing about WHAT, so an archive that lost a file in transit,
    or had one edited afterwards, read as intact. An operator running a
    postmortem from an archive is reading evidence, and evidence that cannot be
    checked is a claim (#5443).
    """

    created_at: str
    bernstein_version: str
    run_id: str | None
    file_count: int
    total_size_bytes: int
    sections: list[str] = field(default_factory=lambda: list[str]())
    #: ``{archive member name: sha256 hex of its bytes}``. Empty on an archive
    #: written before hashes existed, which `verify_archive` reports as
    #: unverifiable rather than as passing.
    files: dict[str, str] = field(default_factory=lambda: dict[str, str]())


@dataclass(frozen=True)
class ArchiveVerification:
    """The result of checking an archive against its own manifest."""

    ok: bool
    #: Members whose bytes no longer hash to what the manifest recorded.
    modified: list[str] = field(default_factory=lambda: list[str]())
    #: Members the manifest lists that are not in the archive.
    missing: list[str] = field(default_factory=lambda: list[str]())
    #: Members in the archive that the manifest does not list.
    unexpected: list[str] = field(default_factory=lambda: list[str]())
    #: Set when the archive cannot be checked at all, rather than failing a check.
    unverifiable: str | None = None


# ---------------------------------------------------------------------------
# Section definitions
# ---------------------------------------------------------------------------

ARCHIVE_SECTIONS: dict[str, list[str]] = {
    "tasks": [".sdd/tasks/*.jsonl"],
    "logs": [".sdd/runtime/*.log"],
    "costs": [".sdd/runtime/costs/*.json"],
    "audit": [".sdd/audit/*.jsonl"],
    "metrics": [".sdd/metrics/*.jsonl"],
    "traces": [".sdd/traces/*.json"],
    "config": [".sdd/config/*", "bernstein.yaml"],
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def collect_archive_files(
    base_dir: Path,
    sections: list[str] | None = None,
) -> list[Path]:
    """Collect files matching the requested archive sections.

    Args:
        base_dir: Project root directory.
        sections: Optional list of section names (keys of
            ``ARCHIVE_SECTIONS``).  When *None*, all sections are included.

    Returns:
        De-duplicated, sorted list of matching file paths.
    """
    chosen = sections if sections is not None else list(ARCHIVE_SECTIONS)
    seen: set[Path] = set()
    result: list[Path] = []

    for section in chosen:
        patterns = ARCHIVE_SECTIONS.get(section, [])
        for pattern in patterns:
            full_pattern = str(base_dir / pattern)
            for match in _glob.glob(full_pattern):
                path = Path(match)
                if path.is_file() and path not in seen:
                    seen.add(path)
                    result.append(path)

    result.sort()
    return result


def create_archive(
    base_dir: Path,
    output_path: Path,
    sections: list[str] | None = None,
) -> ArchiveManifest:
    """Create a ZIP archive of the requested run sections.

    The archive contains every matched file stored relative to *base_dir*,
    plus a ``manifest.json`` at the archive root.

    Args:
        base_dir: Project root directory.
        output_path: Destination path for the ``.zip`` file.
        sections: Optional list of section names.  *None* means all.

    Returns:
        The :class:`ArchiveManifest` written into the archive.
    """
    files = collect_archive_files(base_dir, sections)
    total_size = sum(f.stat().st_size for f in files)
    chosen_sections = sections if sections is not None else list(ARCHIVE_SECTIONS)
    # One member name per file, computed once and used for BOTH the arcname and
    # the manifest key, so the two cannot drift apart. POSIX-style, because
    # ``str(Path)`` renders ``\\`` on Windows: the same tree would otherwise
    # produce different member names and a different manifest depending on
    # which host wrote it, and an archive exists to be read somewhere else.
    members = [(f, f.relative_to(base_dir).as_posix()) for f in files]
    # Hashed from the bytes actually written, keyed by the name they are written
    # under, so the manifest describes the archive rather than the workspace it
    # came from - the workspace is usually deleted immediately afterwards, which
    # is the whole reason the archive exists.
    digests = {name: _sha256_file(f) for f, name in members}

    # Attempt to read a run-id from .sdd/runtime/run_id, if present.
    run_id: str | None = None
    run_id_path = base_dir / ".sdd" / "runtime" / "run_id"
    if run_id_path.is_file():
        run_id = run_id_path.read_text(encoding="utf-8").strip() or None

    manifest = ArchiveManifest(
        created_at=datetime.now(UTC).isoformat(),
        bernstein_version=bernstein.__version__,
        run_id=run_id,
        file_count=len(files),
        total_size_bytes=total_size,
        sections=sorted(chosen_sections),
        files=digests,
    )

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path, name in members:
            zf.write(file_path, name)

        zf.writestr("manifest.json", json.dumps(asdict(manifest), indent=2) + "\n")

    return manifest


def _sha256_file(path: Path) -> str:
    """sha256 hex of a file, read in chunks so a large trace store does not."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_archive_manifest(archive_path: Path) -> ArchiveManifest:
    """Read the manifest out of an archive.

    Raises:
        ArchiveManifestError: The archive has no manifest, or one that cannot
            be parsed, is not an object, or lacks a required field. All are
            refusals rather than an empty manifest: a missing manifest that
            read as "no files recorded" would verify clean, which is the answer
            an operator must never be given.
    """
    try:
        with zipfile.ZipFile(archive_path) as zf:
            return _manifest_from_zip(zf, archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveManifestError(f"{archive_path} is not a readable archive: {exc}") from exc


def _manifest_from_zip(zf: zipfile.ZipFile, archive_path: Path) -> ArchiveManifest:
    """Parse ``manifest.json`` out of an already open archive."""
    try:
        raw = zf.read("manifest.json").decode("utf-8")
    except KeyError as exc:
        raise ArchiveManifestError(f"{archive_path} has no manifest.json") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ArchiveManifestError(f"{archive_path} has an unreadable manifest.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArchiveManifestError(f"{archive_path} manifest.json is not an object")
    known = {f.name for f in fields(ArchiveManifest)}
    try:
        return ArchiveManifest(**{k: v for k, v in payload.items() if k in known})
    except TypeError as exc:
        # Parses, is an object, and still lacks a required field such as
        # ``file_count``: a truncated manifest. Same refusal as the other two
        # ways a manifest can fail to mean something, not a bare traceback.
        raise ArchiveManifestError(f"{archive_path} has an incomplete manifest.json: {exc}") from exc


def verify_archive(archive_path: Path) -> ArchiveVerification:
    """Check every archived member against the hash the manifest recorded.

    Offline and self-contained: the manifest travels inside the archive, so a
    reader holding only the file can answer the question. Nothing here consults
    the workspace the archive came from, which by then usually does not exist.

    Three failure shapes are reported separately rather than as one boolean,
    because they mean different things to whoever has to act: a MODIFIED member
    is evidence that changed after the fact, a MISSING one is evidence that did
    not survive, and an UNEXPECTED one is a member nothing vouches for.

    Args:
        archive_path: The ``.zip`` produced by :func:`create_archive`.

    Returns:
        An :class:`ArchiveVerification`. ``ok`` is ``True`` only when every
        listed member is present and hashes as recorded, and the archive holds
        nothing else.

    Raises:
        ArchiveManifestError: The archive or its manifest cannot be read.
    """
    try:
        with zipfile.ZipFile(archive_path) as zf:
            return _verify_open_archive(zf, archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveManifestError(f"{archive_path} is not a readable archive: {exc}") from exc


def _verify_open_archive(zf: zipfile.ZipFile, archive_path: Path) -> ArchiveVerification:
    """`verify_archive` over one open handle, so the manifest and members come from one read."""
    manifest = _manifest_from_zip(zf, archive_path)
    if not manifest.files:
        # An archive written before hashes existed. Reporting it as verified
        # would claim a check that never ran.
        return ArchiveVerification(
            ok=False,
            unverifiable=(
                f"{archive_path} carries no per-file hashes (written by bernstein "
                f"{manifest.bernstein_version or 'unknown'}); its contents cannot be checked"
            ),
        )
    if manifest.file_count != len(manifest.files):
        # The writer sets both from the same list, so a disagreement means the
        # manifest itself was edited or damaged, and nothing it vouches for can
        # be taken at its word.
        return ArchiveVerification(
            ok=False,
            unverifiable=(
                f"{archive_path} manifest contradicts itself: file_count is "
                f"{manifest.file_count} but it lists {len(manifest.files)} hashes"
            ),
        )

    modified: list[str] = []
    missing: list[str] = []
    present = {name for name in zf.namelist() if not name.endswith("/")}
    for name, expected in sorted(manifest.files.items()):
        if name not in present:
            missing.append(name)
            continue
        digest = hashlib.sha256()
        try:
            with zf.open(name) as member:
                for chunk in iter(lambda: member.read(_HASH_CHUNK_BYTES), b""):
                    digest.update(chunk)
        except zipfile.BadZipFile:
            # zipfile's own CRC check fired: the bytes changed without the
            # member's header being rewritten. Still a modified member.
            modified.append(name)
            continue
        if digest.hexdigest() != expected:
            modified.append(name)

    unexpected = sorted(present - set(manifest.files) - {"manifest.json"})
    return ArchiveVerification(
        ok=not (modified or missing or unexpected),
        modified=modified,
        missing=missing,
        unexpected=unexpected,
    )


def format_verification(archive_path: Path, result: ArchiveVerification) -> str:
    """A verification result as an operator-facing report."""
    if result.unverifiable is not None:
        return f"UNVERIFIABLE  {archive_path}\n  {result.unverifiable}"
    if result.ok:
        return f"OK  {archive_path}"
    lines = [f"FAILED  {archive_path}"]
    for label, names in (
        ("modified since the archive was written", result.modified),
        ("listed in the manifest and absent", result.missing),
        ("present and not listed in the manifest", result.unexpected),
    ):
        for name in names:
            lines.append(f"  {label}: {name}")
    return "\n".join(lines)


def format_archive_summary(manifest: ArchiveManifest) -> str:
    """Return a human-readable summary of an archive manifest.

    Args:
        manifest: The manifest to summarise.

    Returns:
        Multi-line plain-text summary.
    """
    size_kb = manifest.total_size_bytes / 1024
    lines = [
        "Archive Summary",
        "===============",
        f"Created:   {manifest.created_at}",
        f"Version:   {manifest.bernstein_version}",
        f"Run name:  {_archive_run_name(manifest.run_id)}",
        f"Run ID:    {manifest.run_id or '(none)'}",
        f"Files:     {manifest.file_count}",
        f"Size:      {size_kb:.1f} KB",
        f"Sections:  {', '.join(manifest.sections) if manifest.sections else '(none)'}",
    ]
    return "\n".join(lines)


def _archive_run_name(run_id: str | None) -> str:
    """Render the memorable name for an archive run id, if valid."""
    from uuid import UUID

    if not run_id:
        return "(none)"
    try:
        return render_name(UUID(run_id))
    except ValueError:
        return "(unnamed)"
