"""CLI-020: Export full run archive as ZIP.

Collects all run state from ``.sdd/`` (tasks, logs, costs, audit, metrics,
traces, config) plus the top-level ``bernstein.yaml`` and packages them into
a single ZIP archive with an embedded ``manifest.json``.
"""

from __future__ import annotations

import glob as _glob
import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bernstein


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveManifest:
    """Metadata embedded in the archive as ``manifest.json``."""

    created_at: str
    bernstein_version: str
    run_id: str | None
    file_count: int
    total_size_bytes: int
    sections: list[str] = field(default_factory=list)


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

    # Attempt to read a run-id from .sdd/runtime/run_id, if present.
    run_id: str | None = None
    run_id_path = base_dir / ".sdd" / "runtime" / "run_id"
    if run_id_path.is_file():
        run_id = run_id_path.read_text(encoding="utf-8").strip() or None

    manifest = ArchiveManifest(
        created_at=datetime.now(timezone.utc).isoformat(),
        bernstein_version=bernstein.__version__,
        run_id=run_id,
        file_count=len(files),
        total_size_bytes=total_size,
        sections=sorted(chosen_sections),
    )

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            arcname = str(file_path.relative_to(base_dir))
            zf.write(file_path, arcname)

        zf.writestr("manifest.json", json.dumps(asdict(manifest), indent=2) + "\n")

    return manifest


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
        f"Run ID:    {manifest.run_id or '(none)'}",
        f"Files:     {manifest.file_count}",
        f"Size:      {size_kb:.1f} KB",
        f"Sections:  {', '.join(manifest.sections) if manifest.sections else '(none)'}",
    ]
    return "\n".join(lines)
