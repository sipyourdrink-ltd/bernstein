"""Unit tests for :mod:`bernstein.core.persistence.tiles`.

These tests cover the read-only tile interface for immutable segment
snapshots:

* ``tile_path`` resolves the tile location under ``<audit_dir>/tiles/``.
* ``has_tile`` reflects whether a tile exists for a segment.
* ``read_tile`` returns the tile bytes, or ``None`` when absent - never
  raising.
* ``list_tile_segments`` returns sorted segment names, empty when none.
* No read function writes to the filesystem (read-only compatible).
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.persistence.tiles import (
    TILES_SUBDIR,
    has_tile,
    list_tile_segments,
    read_tile,
    tile_path,
)


def test_tile_path_returns_correct_path(tmp_path: Path) -> None:
    assert tile_path(tmp_path, "2026-08-24.jsonl") == tmp_path / TILES_SUBDIR / "2026-08-24.jsonl"


def test_has_tile_false_when_no_tiles_exist(tmp_path: Path) -> None:
    assert has_tile(tmp_path, "2026-08-24.jsonl") is False


def test_has_tile_true_after_creating_one(tmp_path: Path) -> None:
    path = tile_path(tmp_path, "2026-08-24.jsonl")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"sealed")
    assert has_tile(tmp_path, "2026-08-24.jsonl") is True


def test_read_tile_returns_none_for_missing_tile(tmp_path: Path) -> None:
    assert read_tile(tmp_path, "2026-08-24.jsonl") is None


def test_read_tile_returns_bytes_for_existing_tile(tmp_path: Path) -> None:
    path = tile_path(tmp_path, "2026-08-24.jsonl")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"sealed-bytes")
    assert read_tile(tmp_path, "2026-08-24.jsonl") == b"sealed-bytes"


def test_list_tile_segments_empty_when_no_tiles(tmp_path: Path) -> None:
    assert list_tile_segments(tmp_path) == []


def test_list_tile_segments_returns_sorted_names(tmp_path: Path) -> None:
    tiles_dir = tmp_path / TILES_SUBDIR
    tiles_dir.mkdir(parents=True)
    for name in ("2026-08-26.jsonl", "2026-08-24.jsonl", "2026-08-25.jsonl"):
        (tiles_dir / name).write_bytes(b"x")
    assert list_tile_segments(tmp_path) == [
        "2026-08-24.jsonl",
        "2026-08-25.jsonl",
        "2026-08-26.jsonl",
    ]


def test_read_functions_do_not_write(tmp_path: Path) -> None:
    """Read functions must not create any files or directories."""
    read_tile(tmp_path, "2026-08-24.jsonl")
    has_tile(tmp_path, "2026-08-24.jsonl")
    list_tile_segments(tmp_path)
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
