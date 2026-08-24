"""Immutable segment snapshots (tiles) for read-only audit verification.

``bernstein audit verify`` reads live segments (``*.jsonl``) that a writer may
be appending to. A long full-history verify can therefore observe a segment
mid-append and report a transient failure that is not a real integrity
problem. Tiles eliminate this race: the seal job (slice 1, out of scope here)
writes a byte-for-byte copy of each segment as it existed at seal time, and
the verifier reads those immutable tiles instead of the mutable live files.

A tile is stored at ``<audit_dir>/tiles/<segment_name>`` - the same name as
the live file (e.g. ``2026-08-24.jsonl``) - and holds the segment content as
it existed at seal time (up to ``byte_len`` from the Merkle seal). Tiles are
immutable after creation.

This module defines only the READ side. Every function here is pure and
never writes to the filesystem, so a verifier can run against a read-only
audit directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Subdirectory of the audit dir holding immutable segment snapshots.
TILES_SUBDIR = "tiles"


def tile_path(audit_dir: Path, segment_name: str) -> Path:
    """Return the tile path for *segment_name* under *audit_dir*.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name (e.g. ``2026-08-24.jsonl``).

    Returns:
        ``<audit_dir>/tiles/<segment_name>``.
    """
    return audit_dir / TILES_SUBDIR / segment_name


def has_tile(audit_dir: Path, segment_name: str) -> bool:
    """Return whether a tile exists for *segment_name*.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name.

    Returns:
        ``True`` when the tile file exists, ``False`` otherwise.
    """
    return tile_path(audit_dir, segment_name).is_file()


def read_tile(audit_dir: Path, segment_name: str) -> bytes | None:
    """Read the tile bytes for *segment_name*, or ``None`` if not found.

    Never raises for a missing tile - the caller treats ``None`` as "no
    immutable snapshot available" and falls back to the live segment.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name.

    Returns:
        The tile bytes, or ``None`` if the tile does not exist or is
        unreadable.
    """
    path = tile_path(audit_dir, segment_name)
    try:
        return path.read_bytes()
    except OSError:
        return None


def list_tile_segments(audit_dir: Path) -> list[str]:
    """Return the sorted segment names that have tiles under *audit_dir*.

    Args:
        audit_dir: The audit directory.

    Returns:
        Sorted list of segment names with tiles, empty when none exist.
    """
    tiles_dir = audit_dir / TILES_SUBDIR
    if not tiles_dir.is_dir():
        return []
    return sorted(p.name for p in tiles_dir.iterdir() if p.is_file())


__all__ = [
    "TILES_SUBDIR",
    "has_tile",
    "list_tile_segments",
    "read_tile",
    "tile_path",
]
