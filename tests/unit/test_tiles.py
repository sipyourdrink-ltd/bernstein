"""Unit tests for :mod:`bernstein.core.persistence.tiles`.

These tests cover the read-only tile interface for immutable segment
snapshots:

* ``tile_path`` resolves the tile location under ``<audit_dir>/tiles/``.
* ``has_tile`` reflects whether a tile exists for a segment.
* ``read_tile`` returns the tile bytes, or ``None`` when absent - never
  raising.
* ``list_tile_segments`` returns sorted segment names, empty when none.
* No read function writes to the filesystem (read-only compatible).

Hash-tile tests cover the write side (``generate_tiles``) for content-
addressed audit segment manifests (issue #3829, slice 1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.persistence.tiles import (
    TILES_SUBDIR,
    generate_tiles,
    has_hash_tile,
    has_tile,
    list_hash_tiles,
    list_tile_segments,
    read_hash_tile,
    read_tile,
    tile_hash_path,
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


# ---------------------------------------------------------------------------
# Hash-tile write-side tests (generate_tiles, issue #3829 slice 1)
# ---------------------------------------------------------------------------


def _make_seal(segments: list[tuple[str, bytes]]) -> dict:
    """Build a minimal seal dict mimicking compute_seal output shape."""
    import hashlib

    leaves = []
    for name, content in segments:
        leaves.append(
            {
                "file": name,
                "hash": hashlib.sha256(b"\x00" + content).hexdigest(),
                "byte_len": len(content),
            }
        )
    return {
        "root_hash": "fake-root",
        "algorithm": "sha256",
        "scheme": 2,
        "leaf_count": len(leaves),
        "leaves": leaves,
        "origin": "",
        "entry_count": 0,
        "sealed_at": 0.0,
        "sealed_at_iso": "2026-08-24T00:00:00Z",
    }


def _write_segments(audit_dir: Path, segments: list[tuple[str, bytes]]) -> None:
    """Write segment files into audit_dir."""
    for name, content in segments:
        (audit_dir / name).write_bytes(content)


def test_byte_identical_across_two_directories(tmp_path: Path) -> None:
    """Two operators with identical segments produce byte-identical tiles."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    segments = [
        ("2026-08-24.jsonl", b'{"event":"a"}\n{"event":"b"}\n'),
        ("2026-08-25.jsonl", b'{"event":"c"}\n'),
    ]
    _write_segments(dir_a, segments)
    _write_segments(dir_b, segments)

    seal = _make_seal(segments)

    generate_tiles(dir_a, seal)
    generate_tiles(dir_b, seal)

    tiles_a = sorted(p.name for p in (dir_a / TILES_SUBDIR).iterdir() if p.suffix == ".tile")
    tiles_b = sorted(p.name for p in (dir_b / TILES_SUBDIR).iterdir() if p.suffix == ".tile")

    assert tiles_a == tiles_b
    assert len(tiles_a) == 2

    for name in tiles_a:
        content_a = (dir_a / TILES_SUBDIR / name).read_bytes()
        content_b = (dir_b / TILES_SUBDIR / name).read_bytes()
        assert content_a == content_b, f"Tile {name} differs across directories"


def test_immutability_existing_tiles_not_rewritten(tmp_path: Path) -> None:
    """Appending to a segment does not rewrite its previously-written tile."""
    segments = [("2026-08-24.jsonl", b'{"event":"a"}\n')]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    generate_tiles(tmp_path, seal)

    tile_file = tile_hash_path(tmp_path, "2026-08-24.jsonl")
    original_content = tile_file.read_bytes()
    original_mtime = tile_file.stat().st_mtime_ns

    # Append more data to the segment
    (tmp_path / "2026-08-24.jsonl").write_bytes(b'{"event":"a"}\n{"event":"b"}\n')

    # Regenerate tiles with the SAME seal (same byte_len as original)
    generate_tiles(tmp_path, seal)

    assert tile_file.read_bytes() == original_content
    assert tile_file.stat().st_mtime_ns == original_mtime


def test_idempotency_running_twice_writes_nothing(tmp_path: Path) -> None:
    """Running generate_tiles twice with same input writes nothing the second time."""
    segments = [
        ("2026-08-24.jsonl", b'{"event":"a"}\n'),
        ("2026-08-25.jsonl", b'{"event":"b"}\n'),
    ]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    written1 = generate_tiles(tmp_path, seal)
    assert len(written1) == 2

    files_after_first = sorted(p.name for p in (tmp_path / TILES_SUBDIR).iterdir())
    contents_after_first = {p.name: p.read_bytes() for p in (tmp_path / TILES_SUBDIR).iterdir()}

    written2 = generate_tiles(tmp_path, seal)
    assert len(written2) == 0

    files_after_second = sorted(p.name for p in (tmp_path / TILES_SUBDIR).iterdir())
    contents_after_second = {p.name: p.read_bytes() for p in (tmp_path / TILES_SUBDIR).iterdir()}

    assert files_after_first == files_after_second
    assert contents_after_first == contents_after_second


def test_naming_no_conflicts_lowercase_tile_suffix(tmp_path: Path) -> None:
    """Tile filenames use .tile suffix and never collide with segment filenames."""
    segments = [
        ("2026-08-24.jsonl", b'{"event":"a"}\n'),
    ]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    generate_tiles(tmp_path, seal)

    tiles_dir = tmp_path / TILES_SUBDIR
    tile_names = [p.name for p in tiles_dir.iterdir() if p.is_file()]
    segment_names = [p.name for p in tmp_path.iterdir() if p.is_file()]

    for tn in tile_names:
        assert tn.endswith(".tile"), f"Tile filename {tn} does not end with .tile"
        assert tn not in segment_names, f"Tile filename {tn} collides with a segment filename"


def test_conflicting_leaf_hash_raises_value_error(tmp_path: Path) -> None:
    """A tile with a different leaf_hash must raise ValueError, not silently rewrite."""
    segments = [("2026-08-24.jsonl", b'{"event":"a"}\n')]
    _write_segments(tmp_path, segments)

    seal1 = _make_seal(segments)
    generate_tiles(tmp_path, seal1)

    # Build a seal with a different leaf_hash for the same segment
    import hashlib

    different_content = b'{"event":"different"}\n'
    different_hash = hashlib.sha256(b"\x00" + different_content).hexdigest()
    seal2 = {
        "root_hash": "fake-root-2",
        "algorithm": "sha256",
        "scheme": 2,
        "leaf_count": 1,
        "leaves": [{"file": "2026-08-24.jsonl", "hash": different_hash, "byte_len": len(different_content)}],
        "origin": "",
        "entry_count": 0,
        "sealed_at": 0.0,
        "sealed_at_iso": "2026-08-24T00:00:00Z",
    }

    with pytest.raises(ValueError, match="different leaf_hash"):
        generate_tiles(tmp_path, seal2)


def test_generate_tiles_creates_tiles_dir(tmp_path: Path) -> None:
    """generate_tiles creates the tiles subdirectory if it does not exist."""
    segments = [("2026-08-24.jsonl", b'{"event":"a"}\n')]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    assert not (tmp_path / TILES_SUBDIR).exists()
    generate_tiles(tmp_path, seal)
    assert (tmp_path / TILES_SUBDIR).is_dir()


def test_generate_tiles_content_sha256_correct(tmp_path: Path) -> None:
    """The content_sha256 field is the plain SHA-256 of the sealed byte prefix."""
    import hashlib

    content = b'{"event":"a"}\n{"event":"b"}\n'
    segments = [("2026-08-24.jsonl", content)]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    generate_tiles(tmp_path, seal)

    tile = read_hash_tile(tmp_path, "2026-08-24.jsonl")
    assert tile is not None
    assert tile["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert tile["byte_len"] == len(content)
    assert tile["algorithm"] == "sha256"
    assert tile["scheme"] == 2
    assert tile["segment"] == "2026-08-24.jsonl"


def test_generate_tiles_byte_len_prefix_hashing(tmp_path: Path) -> None:
    """When byte_len is less than file size, only the prefix is hashed."""
    import hashlib

    full_content = b'{"event":"a"}\n{"event":"b"}\n'
    segments = [("2026-08-24.jsonl", full_content)]
    _write_segments(tmp_path, segments)

    # Seal pins only the first 13 bytes (the first line)
    prefix_len = 13
    prefix = full_content[:prefix_len]
    leaf_hash = hashlib.sha256(b"\x00" + prefix).hexdigest()

    seal = {
        "root_hash": "fake-root",
        "algorithm": "sha256",
        "scheme": 2,
        "leaf_count": 1,
        "leaves": [{"file": "2026-08-24.jsonl", "hash": leaf_hash, "byte_len": prefix_len}],
        "origin": "",
        "entry_count": 0,
        "sealed_at": 0.0,
        "sealed_at_iso": "2026-08-24T00:00:00Z",
    }

    generate_tiles(tmp_path, seal)

    tile = read_hash_tile(tmp_path, "2026-08-24.jsonl")
    assert tile is not None
    assert tile["content_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert tile["byte_len"] == prefix_len


def test_generate_tiles_no_byte_len_hashes_full_file(tmp_path: Path) -> None:
    """When byte_len is absent from the seal leaf, the entire file is hashed."""
    import hashlib

    content = b'{"event":"a"}\n'
    segments = [("2026-08-24.jsonl", content)]
    _write_segments(tmp_path, segments)

    leaf_hash = hashlib.sha256(b"\x00" + content).hexdigest()
    seal = {
        "root_hash": "fake-root",
        "algorithm": "sha256",
        "scheme": 2,
        "leaf_count": 1,
        "leaves": [{"file": "2026-08-24.jsonl", "hash": leaf_hash}],
        "origin": "",
        "entry_count": 0,
        "sealed_at": 0.0,
        "sealed_at_iso": "2026-08-24T00:00:00Z",
    }

    generate_tiles(tmp_path, seal)

    tile = read_hash_tile(tmp_path, "2026-08-24.jsonl")
    assert tile is not None
    assert tile["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert tile["byte_len"] == len(content)


def test_generate_tiles_atomic_write_no_tmp_left(tmp_path: Path) -> None:
    """No .tmp files remain after generate_tiles."""
    segments = [("2026-08-24.jsonl", b'{"event":"a"}\n')]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    generate_tiles(tmp_path, seal)

    tiles_dir = tmp_path / TILES_SUBDIR
    tmp_files = [p for p in tiles_dir.iterdir() if p.name.endswith(".tmp")]
    assert len(tmp_files) == 0


def test_list_hash_tiles_returns_sorted_names(tmp_path: Path) -> None:
    """list_hash_tiles returns sorted .tile file names."""
    segments = [
        ("2026-08-26.jsonl", b"x\n"),
        ("2026-08-24.jsonl", b"y\n"),
        ("2026-08-25.jsonl", b"z\n"),
    ]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    generate_tiles(tmp_path, seal)

    result = list_hash_tiles(tmp_path)
    assert result == [
        "2026-08-24.jsonl.tile",
        "2026-08-25.jsonl.tile",
        "2026-08-26.jsonl.tile",
    ]


def test_has_hash_tile(tmp_path: Path) -> None:
    """has_hash_tile reflects existence of a .tile file."""
    segments = [("2026-08-24.jsonl", b'{"event":"a"}\n')]
    _write_segments(tmp_path, segments)
    seal = _make_seal(segments)

    assert has_hash_tile(tmp_path, "2026-08-24.jsonl") is False
    generate_tiles(tmp_path, seal)
    assert has_hash_tile(tmp_path, "2026-08-24.jsonl") is True


def test_generate_tiles_empty_seal(tmp_path: Path) -> None:
    """An empty seal (no leaves) produces no tiles and does not error."""
    seal = {
        "root_hash": "empty",
        "algorithm": "sha256",
        "scheme": 2,
        "leaf_count": 0,
        "leaves": [],
        "origin": "",
        "entry_count": 0,
        "sealed_at": 0.0,
        "sealed_at_iso": "2026-08-24T00:00:00Z",
    }
    result = generate_tiles(tmp_path, seal)
    assert result == []
