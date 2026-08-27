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

Hash tiles (slice 1, issue #3829) are content-addressed JSON manifests stored
at ``<audit_dir>/tiles/<segment_name>.tile``. They record the raw
``SHA-256`` of the sealed byte prefix so a future verifier can check content
without re-deriving the domain-separated leaf.

This module defines the READ side (snapshot tiles) and the WRITE side
(hash tiles). Snapshot read functions remain pure and never write.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

#: Subdirectory of the audit dir holding immutable segment snapshots.
TILES_SUBDIR = "tiles"


def tile_path(audit_dir: Path, segment_name: str, suffix: str = "") -> Path:
    """Return the tile path for *segment_name* under *audit_dir*.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name (e.g. ``2026-08-24.jsonl``).
        suffix: Optional suffix appended to the file name (e.g. ``".tile"``).

    Returns:
        ``<audit_dir>/tiles/<segment_name><suffix>``.
    """
    return audit_dir / TILES_SUBDIR / f"{segment_name}{suffix}"


def tile_hash_path(audit_dir: Path, segment_name: str) -> Path:
    """Return the hash-tile path for *segment_name*.

    Hash tiles use the ``.tile`` suffix so they never collide with snapshot
    tiles or live segment files.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name.

    Returns:
        ``<audit_dir>/tiles/<segment_name>.tile``.
    """
    return tile_path(audit_dir, segment_name, suffix=".tile")


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

    Hash tiles (``*.tile``) are excluded so this continues to report only
    snapshot tiles.

    Args:
        audit_dir: The audit directory.

    Returns:
        Sorted list of segment names with tiles, empty when none exist.
    """
    tiles_dir = audit_dir / TILES_SUBDIR
    if not tiles_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in tiles_dir.iterdir()
        if p.is_file() and not p.name.endswith(".tile") and not p.name.endswith(".tmp")
    )


def list_hash_tiles(audit_dir: Path) -> list[str]:
    """Return sorted hash-tile file names (``*.tile``) under *audit_dir*.

    Args:
        audit_dir: The audit directory.

    Returns:
        Sorted list of hash-tile file names, empty when none exist.
    """
    tiles_dir = audit_dir / TILES_SUBDIR
    if not tiles_dir.is_dir():
        return []
    return sorted(p.name for p in tiles_dir.iterdir() if p.is_file() and p.name.endswith(".tile"))


def has_hash_tile(audit_dir: Path, segment_name: str) -> bool:
    """Return whether a hash tile exists for *segment_name*.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name.

    Returns:
        True when ``<segment>.tile`` exists.
    """
    return tile_hash_path(audit_dir, segment_name).is_file()


def read_hash_tile(audit_dir: Path, segment_name: str) -> dict[str, Any] | None:
    """Read the hash tile JSON for *segment_name*, or ``None`` if absent.

    Args:
        audit_dir: The audit directory.
        segment_name: Live segment file name.

    Returns:
        Parsed tile dict, or ``None`` if missing or unreadable.
    """
    path = tile_hash_path(audit_dir, segment_name)
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def generate_tiles(audit_dir: Path, seal: dict[str, Any]) -> list[Path]:
    """Write content-addressed hash tiles for every leaf in *seal*.

    Each leaf in ``seal["leaves"]`` produces a JSON file at
    ``<audit_dir>/tiles/<segment>.tile`` with shape::

        {
            "segment": "2026-08-24.jsonl",
            "leaf_hash": "<hex from seal>",
            "byte_len": 1234,
            "content_sha256": "<plain SHA-256 of prefix>",
            "algorithm": "sha256",
            "scheme": 2
        }

    The ``content_sha256`` is a plain ``SHA-256`` over the first
    ``byte_len`` bytes of the segment file (no domain separation). When
    ``byte_len`` is absent the entire file is hashed.

    Tiles are written atomically via ``<name>.tile.tmp`` + ``os.replace``.
    If a tile already exists with the same ``leaf_hash`` it is left
    untouched (idempotent). If it exists with a different ``leaf_hash`` a
    ``ValueError`` is raised and no rewrite occurs.

    Args:
        audit_dir: Audit directory containing the live ``*.jsonl`` segments.
        seal: Seal dict as produced by
            :func:`bernstein.core.persistence.merkle.compute_seal`.

    Returns:
        List of paths that were newly written in this call (empty on a
        fully idempotent re-run).

    Raises:
        ValueError: If an existing tile carries a different ``leaf_hash``.
    """
    leaves = seal.get("leaves", [])
    if not isinstance(leaves, list):
        return []

    raw_scheme = seal.get("scheme", 2)
    if isinstance(raw_scheme, bool) or not isinstance(raw_scheme, int):
        try:
            raw_scheme = int(str(raw_scheme))
        except (ValueError, TypeError):
            raw_scheme = 2
    scheme: int = int(raw_scheme)

    tiles_dir = audit_dir / TILES_SUBDIR
    tiles_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    for leaf in leaves:
        if not isinstance(leaf, dict):
            continue
        segment = leaf.get("file")
        leaf_hash = leaf.get("hash")
        if not isinstance(segment, str) or not isinstance(leaf_hash, str):
            continue

        dest = tile_hash_path(audit_dir, segment)

        byte_len_raw = leaf.get("byte_len")
        byte_len: int | None = None
        if isinstance(byte_len_raw, int) and not isinstance(byte_len_raw, bool) and byte_len_raw >= 0:
            byte_len = byte_len_raw

        segment_path = audit_dir / segment
        if byte_len is not None:
            try:
                full = segment_path.read_bytes()
            except OSError as exc:
                msg = f"Segment file missing for tile: {segment_path}"
                raise ValueError(msg) from exc
            data = full[:byte_len]
            tile_byte_len = byte_len
        else:
            try:
                data = segment_path.read_bytes()
            except OSError as exc:
                msg = f"Segment file missing for tile: {segment_path}"
                raise ValueError(msg) from exc
            tile_byte_len = len(data)

        content_sha256 = hashlib.sha256(data).hexdigest()

        tile_obj: dict[str, Any] = {
            "segment": segment,
            "leaf_hash": leaf_hash,
            "byte_len": tile_byte_len,
            "content_sha256": content_sha256,
            "algorithm": "sha256",
            "scheme": scheme,
        }

        if dest.is_file():
            try:
                existing: dict[str, Any] = json.loads(dest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                msg = f"Existing tile is not valid JSON: {dest}"
                raise ValueError(msg) from exc
            if existing.get("leaf_hash") == leaf_hash and existing.get("byte_len") == tile_byte_len:
                # Same prefix, same hash: the tile already is what this seal
                # would write.
                continue
            if existing.get("byte_len") == tile_byte_len:
                # The same bytes hashing two ways is the one thing a tile
                # exists to catch, so it stays fatal.
                msg = (
                    f"Tile exists for the same {tile_byte_len} bytes with a different "
                    f"leaf_hash: {dest} (existing={existing.get('leaf_hash')!r} new={leaf_hash!r})"
                )
                raise ValueError(msg)
            # Different length: the live segment grew between seals, which is
            # what a live segment does. Refusing that failed the second seal
            # of a run outright. The tile describes the prefix this seal
            # covers, so the newer seal replaces it.

        tmp_path = dest.parent / (dest.name + ".tmp")
        tmp_path.write_text(json.dumps(tile_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, dest)
        written.append(dest)

    return written


__all__ = [
    "TILES_SUBDIR",
    "generate_tiles",
    "has_hash_tile",
    "has_tile",
    "list_hash_tiles",
    "list_tile_segments",
    "read_hash_tile",
    "read_tile",
    "tile_hash_path",
    "tile_path",
]
