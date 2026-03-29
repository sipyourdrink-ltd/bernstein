"""Tests for bernstein.core.merkle — Merkle-tree audit seal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.merkle import (
    _combine_hashes,
    _sha256,
    build_merkle_tree,
    compute_seal,
    file_leaf_hash,
    load_latest_seal,
    save_seal,
    verify_merkle,
)

# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------


class TestHashPrimitives:
    def test_sha256_deterministic(self) -> None:
        assert _sha256(b"hello") == _sha256(b"hello")

    def test_sha256_differs(self) -> None:
        assert _sha256(b"a") != _sha256(b"b")

    def test_combine_hashes_deterministic(self) -> None:
        h1, h2 = _sha256(b"left"), _sha256(b"right")
        assert _combine_hashes(h1, h2) == _combine_hashes(h1, h2)

    def test_combine_hashes_order_matters(self) -> None:
        h1, h2 = _sha256(b"left"), _sha256(b"right")
        assert _combine_hashes(h1, h2) != _combine_hashes(h2, h1)


# ---------------------------------------------------------------------------
# file_leaf_hash
# ---------------------------------------------------------------------------


class TestFileLeafHash:
    def test_plain_file(self, tmp_path: Path) -> None:
        f = tmp_path / "log.jsonl"
        f.write_text('{"event": "task.created"}\n')
        h = file_leaf_hash(f)
        assert isinstance(h, str) and len(h) == 64  # SHA-256 hex

    def test_hmac_chained_file_uses_final_hmac(self, tmp_path: Path) -> None:
        f = tmp_path / "log.jsonl"
        lines = [
            json.dumps({"event": "a", "hmac": "aaa111"}),
            json.dumps({"event": "b", "hmac": "bbb222"}),
            json.dumps({"event": "c", "hmac": "final_hmac_value"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert file_leaf_hash(f) == "final_hmac_value"

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        h = file_leaf_hash(f)
        assert h == _sha256(b"empty")

    def test_non_json_file(self, tmp_path: Path) -> None:
        f = tmp_path / "raw.jsonl"
        f.write_text("not json at all\n")
        h = file_leaf_hash(f)
        assert isinstance(h, str) and len(h) == 64


# ---------------------------------------------------------------------------
# build_merkle_tree
# ---------------------------------------------------------------------------


class TestBuildMerkleTree:
    def test_empty_leaves(self) -> None:
        tree = build_merkle_tree([])
        assert tree.leaf_count == 0
        assert tree.root.hash == _sha256(b"empty-tree")

    def test_single_leaf(self) -> None:
        tree = build_merkle_tree([("file.jsonl", "abc123")])
        assert tree.leaf_count == 1
        # Single leaf: the leaf itself becomes the root
        assert tree.root.hash == "abc123"

    def test_two_leaves(self) -> None:
        tree = build_merkle_tree([("a.jsonl", "aaa"), ("b.jsonl", "bbb")])
        assert tree.leaf_count == 2
        expected = _combine_hashes("aaa", "bbb")
        assert tree.root.hash == expected

    def test_three_leaves_odd_padding(self) -> None:
        tree = build_merkle_tree(
            [
                ("a.jsonl", "aaa"),
                ("b.jsonl", "bbb"),
                ("c.jsonl", "ccc"),
            ]
        )
        assert tree.leaf_count == 3
        # Level 1: combine(aaa, bbb), combine(ccc, ccc)  (odd -> duplicate last)
        left = _combine_hashes("aaa", "bbb")
        right = _combine_hashes("ccc", "ccc")
        expected = _combine_hashes(left, right)
        assert tree.root.hash == expected

    def test_four_leaves(self) -> None:
        tree = build_merkle_tree(
            [
                ("a.jsonl", "a1"),
                ("b.jsonl", "b2"),
                ("c.jsonl", "c3"),
                ("d.jsonl", "d4"),
            ]
        )
        assert tree.leaf_count == 4
        left = _combine_hashes("a1", "b2")
        right = _combine_hashes("c3", "d4")
        expected = _combine_hashes(left, right)
        assert tree.root.hash == expected

    def test_deterministic(self) -> None:
        leaves = [("a.jsonl", "x"), ("b.jsonl", "y"), ("c.jsonl", "z")]
        t1 = build_merkle_tree(leaves)
        t2 = build_merkle_tree(leaves)
        assert t1.root.hash == t2.root.hash

    def test_order_sensitive(self) -> None:
        t1 = build_merkle_tree([("a.jsonl", "x"), ("b.jsonl", "y")])
        t2 = build_merkle_tree([("b.jsonl", "y"), ("a.jsonl", "x")])
        assert t1.root.hash != t2.root.hash

    def test_leaves_stored(self) -> None:
        tree = build_merkle_tree([("a.jsonl", "aaa"), ("b.jsonl", "bbb")])
        assert len(tree.leaves) == 2
        assert tree.leaves[0].leaf_path == "a.jsonl"
        assert tree.leaves[1].leaf_path == "b.jsonl"


# ---------------------------------------------------------------------------
# compute_seal
# ---------------------------------------------------------------------------


class TestComputeSeal:
    def test_basic_seal(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit"
        audit.mkdir()
        (audit / "2026-03-28.jsonl").write_text('{"event":"a","hmac":"h1"}\n')
        (audit / "2026-03-29.jsonl").write_text('{"event":"b","hmac":"h2"}\n')

        tree, seal = compute_seal(audit)
        assert seal["root_hash"] == tree.root.hash
        assert seal["leaf_count"] == 2
        assert seal["algorithm"] == "sha256"
        assert len(seal["leaves"]) == 2  # type: ignore[arg-type]
        assert seal["sealed_at"] is not None

    def test_no_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            compute_seal(tmp_path / "nonexistent")

    def test_no_files_raises(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit"
        audit.mkdir()
        with pytest.raises(ValueError, match="No audit log files"):
            compute_seal(audit)

    def test_sorted_order(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit"
        audit.mkdir()
        # Write files out of alphabetical order
        (audit / "z.jsonl").write_text('{"x":1}\n')
        (audit / "a.jsonl").write_text('{"x":2}\n')

        _, seal = compute_seal(audit)
        leaves = seal["leaves"]
        assert leaves[0]["file"] == "a.jsonl"  # type: ignore[index]
        assert leaves[1]["file"] == "z.jsonl"  # type: ignore[index]


# ---------------------------------------------------------------------------
# save_seal / load_latest_seal
# ---------------------------------------------------------------------------


class TestSealPersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        merkle_dir = tmp_path / "merkle"
        seal = {
            "root_hash": "abc",
            "algorithm": "sha256",
            "leaf_count": 1,
            "leaves": [{"file": "f.jsonl", "hash": "h"}],
            "sealed_at": 1.0,
            "sealed_at_iso": "2026-03-29T00:00:00Z",
        }
        path = save_seal(seal, merkle_dir)
        assert path.exists()
        assert path.parent == merkle_dir

        loaded = load_latest_seal(merkle_dir)
        assert loaded is not None
        data, loaded_path = loaded
        assert data["root_hash"] == "abc"
        assert loaded_path == path

    def test_load_no_dir(self, tmp_path: Path) -> None:
        assert load_latest_seal(tmp_path / "nope") is None

    def test_load_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "merkle"
        d.mkdir()
        assert load_latest_seal(d) is None


# ---------------------------------------------------------------------------
# verify_merkle — the core security property
# ---------------------------------------------------------------------------


def _setup_audit(tmp_path: Path) -> tuple[Path, Path]:
    """Create an audit dir with two log files and compute a seal."""
    audit = tmp_path / "audit"
    audit.mkdir()
    merkle = audit / "merkle"

    (audit / "2026-03-28.jsonl").write_text('{"event":"a","hmac":"h1"}\n')
    (audit / "2026-03-29.jsonl").write_text('{"event":"b","hmac":"h2"}\n')

    _, seal = compute_seal(audit)
    save_seal(seal, merkle)
    return audit, merkle


class TestVerifyMerkle:
    def test_clean_verify_passes(self, tmp_path: Path) -> None:
        audit, merkle = _setup_audit(tmp_path)
        result = verify_merkle(audit, merkle)
        assert result.valid
        assert result.errors == []
        assert result.root_hash != ""

    def test_deleted_file_detected(self, tmp_path: Path) -> None:
        audit, merkle = _setup_audit(tmp_path)
        (audit / "2026-03-28.jsonl").unlink()

        result = verify_merkle(audit, merkle)
        assert not result.valid
        assert any("DELETED" in e and "2026-03-28" in e for e in result.errors)

    def test_inserted_file_detected(self, tmp_path: Path) -> None:
        audit, merkle = _setup_audit(tmp_path)
        (audit / "2026-03-30.jsonl").write_text('{"event":"new"}\n')

        result = verify_merkle(audit, merkle)
        assert not result.valid
        assert any("INSERTED" in e and "2026-03-30" in e for e in result.errors)

    def test_tampered_file_detected(self, tmp_path: Path) -> None:
        audit, merkle = _setup_audit(tmp_path)
        (audit / "2026-03-29.jsonl").write_text('{"event":"EVIL","hmac":"tampered"}\n')

        result = verify_merkle(audit, merkle)
        assert not result.valid
        assert any("TAMPERED" in e and "2026-03-29" in e for e in result.errors)

    def test_no_seal_returns_error(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit"
        audit.mkdir()
        merkle = audit / "merkle"

        result = verify_merkle(audit, merkle)
        assert not result.valid
        assert any("No Merkle seal found" in e for e in result.errors)

    def test_resealing_after_new_file(self, tmp_path: Path) -> None:
        """After adding a file and resealing, verification should pass."""
        audit, merkle = _setup_audit(tmp_path)
        (audit / "2026-03-30.jsonl").write_text('{"event":"new"}\n')

        # Old seal should fail
        result = verify_merkle(audit, merkle)
        assert not result.valid

        # Reseal
        _, new_seal = compute_seal(audit)
        save_seal(new_seal, merkle)

        # New seal should pass
        result = verify_merkle(audit, merkle)
        assert result.valid


# ---------------------------------------------------------------------------
# Single-leaf edge case
# ---------------------------------------------------------------------------


class TestSingleFileSeal:
    def test_single_file_seal_and_verify(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit"
        audit.mkdir()
        merkle = audit / "merkle"
        (audit / "only.jsonl").write_text('{"event":"solo"}\n')

        _, seal = compute_seal(audit)
        save_seal(seal, merkle)

        result = verify_merkle(audit, merkle)
        assert result.valid
