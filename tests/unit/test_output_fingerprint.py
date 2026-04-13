"""Tests for agent output fingerprinting (ROAD-167)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.output_fingerprint import (
    CorpusIndex,
    FingerprintConfig,
    FingerprintMatch,
    FingerprintResult,
    MinHash,
    _hash_shingle,
    _normalize_code,
    _tokenize,
    check_fingerprint,
    compute_minhash,
    extract_code_from_diff,
)

# ---------------------------------------------------------------------------
# _normalize_code
# ---------------------------------------------------------------------------


class TestNormalizeCode:
    def test_strips_comments(self) -> None:
        code = "x = 1  # set x\ny = 2"
        result = _normalize_code(code)
        assert "#" not in result
        assert "x = 1" in result

    def test_strips_docstrings(self) -> None:
        code = '"""This is a docstring."""\nx = 1'
        result = _normalize_code(code)
        assert "docstring" not in result
        assert "x = 1" in result

    def test_collapses_whitespace(self) -> None:
        code = "x  =  1\n\n\ny  =  2"
        result = _normalize_code(code)
        assert "  " not in result

    def test_lowercases(self) -> None:
        code = "MyClass = True"
        result = _normalize_code(code)
        assert result == "myclass = true"

    def test_empty_string(self) -> None:
        assert _normalize_code("") == ""


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_token_shingles(self) -> None:
        text = "a b c d e"
        shingles = _tokenize(text, ngram_size=3, shingle_type="token")
        assert "a b c" in shingles
        assert "b c d" in shingles
        assert "c d e" in shingles
        assert len(shingles) == 3

    def test_char_shingles(self) -> None:
        text = "abcde"
        shingles = _tokenize(text, ngram_size=3, shingle_type="char")
        assert "abc" in shingles
        assert "bcd" in shingles
        assert "cde" in shingles
        assert len(shingles) == 3

    def test_short_text_token(self) -> None:
        text = "a b"
        shingles = _tokenize(text, ngram_size=5, shingle_type="token")
        assert shingles == {"a b"}

    def test_empty_text(self) -> None:
        shingles = _tokenize("", ngram_size=3, shingle_type="token")
        assert shingles == set()


# ---------------------------------------------------------------------------
# _hash_shingle
# ---------------------------------------------------------------------------


class TestHashShingle:
    def test_deterministic(self) -> None:
        h1 = _hash_shingle("hello world")
        h2 = _hash_shingle("hello world")
        assert h1 == h2

    def test_different_inputs_differ(self) -> None:
        h1 = _hash_shingle("hello")
        h2 = _hash_shingle("world")
        assert h1 != h2

    def test_returns_int(self) -> None:
        h = _hash_shingle("test")
        assert isinstance(h, int)
        assert 0 <= h <= (1 << 32) - 1


# ---------------------------------------------------------------------------
# MinHash
# ---------------------------------------------------------------------------


class TestMinHash:
    def test_identical_sets_high_similarity(self) -> None:
        mh1 = MinHash(num_perm=128)
        mh2 = MinHash(num_perm=128)
        shingles = {"a b c", "b c d", "c d e", "d e f"}
        mh1.update(shingles)
        mh2.update(shingles)
        assert mh1.jaccard(mh2) == pytest.approx(1.0)

    def test_disjoint_sets_low_similarity(self) -> None:
        mh1 = MinHash(num_perm=128)
        mh2 = MinHash(num_perm=128)
        mh1.update({f"a{i}" for i in range(50)})
        mh2.update({f"b{i}" for i in range(50)})
        sim = mh1.jaccard(mh2)
        assert sim < 0.2

    def test_partial_overlap(self) -> None:
        shared = {f"s{i}" for i in range(30)}
        only_a = {f"a{i}" for i in range(20)}
        only_b = {f"b{i}" for i in range(20)}

        mh1 = MinHash(num_perm=256)
        mh2 = MinHash(num_perm=256)
        mh1.update(shared | only_a)
        mh2.update(shared | only_b)
        sim = mh1.jaccard(mh2)
        # True Jaccard: 30 / (30 + 20 + 20) = 0.4286
        assert 0.25 < sim < 0.65

    def test_incompatible_num_perm_raises(self) -> None:
        mh1 = MinHash(num_perm=64)
        mh2 = MinHash(num_perm=128)
        mh1.update({"a"})
        mh2.update({"a"})

        with pytest.raises(ValueError, match="different num_perm"):
            mh1.jaccard(mh2)

    def test_empty_minhash_all_max(self) -> None:
        mh = MinHash(num_perm=16)
        assert all(v == (1 << 32) - 1 for v in mh.hashvalues)

    def test_hashvalues_property(self) -> None:
        mh = MinHash(num_perm=8)
        mh.update({"test"})
        vals = mh.hashvalues
        assert len(vals) == 8
        assert isinstance(vals, list)


# ---------------------------------------------------------------------------
# compute_minhash
# ---------------------------------------------------------------------------


class TestComputeMinHash:
    def test_returns_minhash(self) -> None:
        mh = compute_minhash("def foo(): return 1")
        assert isinstance(mh, MinHash)

    def test_identical_code_same_hash(self) -> None:
        mh1 = compute_minhash("def foo(): return 1")
        mh2 = compute_minhash("def foo(): return 1")
        assert mh1.jaccard(mh2) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# CorpusIndex
# ---------------------------------------------------------------------------


class TestCorpusIndex:
    def test_add_and_size(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5)
        idx = CorpusIndex(cfg)
        assert idx.size == 0
        idx.add("sample1", "def foo(): return 1")
        assert idx.size == 1

    def test_query_exact_match(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5)
        idx = CorpusIndex(cfg)
        code = "def calculate_sum(a, b): return a + b"
        idx.add("util.py", code)
        matches = idx.query(code)
        assert len(matches) >= 1
        assert matches[0].similarity >= 0.9
        assert matches[0].flagged

    def test_query_no_match(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.9)
        idx = CorpusIndex(cfg)
        idx.add("util.py", "def alpha(): pass")
        matches = idx.query("class BetaProcessor: x = 99")
        flagged = [m for m in matches if m.flagged]
        assert len(flagged) == 0

    def test_query_empty_index(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5)
        idx = CorpusIndex(cfg)
        matches = idx.query("def foo(): pass")
        assert matches == []


# ---------------------------------------------------------------------------
# check_fingerprint
# ---------------------------------------------------------------------------


class TestCheckFingerprint:
    def test_disabled_passes(self) -> None:
        cfg = FingerprintConfig(enabled=False)
        idx = CorpusIndex(cfg)
        result = check_fingerprint("def foo(): pass", idx, cfg)
        assert result.passed
        assert "disabled" in result.detail.lower()

    def test_empty_code_passes(self) -> None:
        cfg = FingerprintConfig(enabled=True)
        idx = CorpusIndex(cfg)
        result = check_fingerprint("", idx, cfg)
        assert result.passed
        assert "empty" in result.detail.lower()

    def test_match_above_threshold_fails(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5)
        idx = CorpusIndex(cfg)
        code = "def calculate_total(items): return sum(item.price for item in items)"
        idx.add("oss-lib/utils.py", code)
        result = check_fingerprint(code, idx, cfg)
        assert not result.passed
        assert len(result.matches) >= 1
        assert any(m.flagged for m in result.matches)

    def test_no_match_passes(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.9)
        idx = CorpusIndex(cfg)
        idx.add("other.py", "class Foo: x = 1")
        result = check_fingerprint("def completely_different_function(): return 42", idx, cfg)
        assert result.passed

    def test_blocking_when_configured(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5, block_on_match=True)
        idx = CorpusIndex(cfg)
        code = "def calculate_total(items): return sum(item.price for item in items)"
        idx.add("oss-lib/utils.py", code)
        result = check_fingerprint(code, idx, cfg)
        assert not result.passed
        assert result.blocked

    def test_non_blocking_by_default(self) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5, block_on_match=False)
        idx = CorpusIndex(cfg)
        code = "def calculate_total(items): return sum(item.price for item in items)"
        idx.add("oss-lib/utils.py", code)
        result = check_fingerprint(code, idx, cfg)
        assert not result.passed
        assert not result.blocked


# ---------------------------------------------------------------------------
# FingerprintConfig defaults
# ---------------------------------------------------------------------------


class TestFingerprintConfig:
    def test_defaults(self) -> None:
        cfg = FingerprintConfig()
        assert cfg.enabled is False
        assert cfg.threshold == pytest.approx(0.7)
        assert cfg.num_perm == 128
        assert cfg.ngram_size == 5
        assert cfg.block_on_match is False

    def test_custom_values(self) -> None:
        cfg = FingerprintConfig(
            enabled=True,
            threshold=0.8,
            num_perm=256,
            ngram_size=3,
            block_on_match=True,
        )
        assert cfg.threshold == pytest.approx(0.8)
        assert cfg.num_perm == 256


# ---------------------------------------------------------------------------
# extract_code_from_diff
# ---------------------------------------------------------------------------


class TestExtractCodeFromDiff:
    def test_extracts_added_lines(self) -> None:
        diff = """\
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 existing line
+new_line = 1
+another_new = 2
 unchanged"""
        result = extract_code_from_diff(diff)
        assert "new_line = 1" in result
        assert "another_new = 2" in result
        assert "existing line" not in result

    def test_ignores_file_headers(self) -> None:
        diff = "+++ b/foo.py\n+real code"
        result = extract_code_from_diff(diff)
        assert "+++" not in result
        assert "real code" in result

    def test_empty_diff(self) -> None:
        assert extract_code_from_diff("") == ""


# ---------------------------------------------------------------------------
# FingerprintMatch / FingerprintResult
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_fingerprint_match(self) -> None:
        m = FingerprintMatch(source_label="lib.py", similarity=0.85, flagged=True)
        assert m.source_label == "lib.py"
        assert m.similarity == pytest.approx(0.85)
        assert m.flagged

    def test_fingerprint_result_defaults(self) -> None:
        r = FingerprintResult(passed=True, blocked=False, detail="ok")
        assert r.matches == []
        assert r.errors == []


# ---------------------------------------------------------------------------
# MinHash.signature / from_signature (new persistence helpers)
# ---------------------------------------------------------------------------


class TestMinHashPersistence:
    def test_signature_property_length(self) -> None:
        mh = MinHash(num_perm=64)
        mh.update({"abc", "bcd"})
        sig = mh.signature
        assert len(sig) == 64

    def test_from_signature_roundtrip(self) -> None:
        mh = compute_minhash("hello world")
        restored = MinHash.from_signature(mh.signature)
        assert mh.signature == restored.signature

    def test_roundtripped_minhash_compares_equal(self) -> None:
        code = "def add(a, b): return a + b"
        mh = compute_minhash(code)
        restored = MinHash.from_signature(mh.signature)
        assert mh.jaccard(restored) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# CorpusIndex.add_directory / save / load
# ---------------------------------------------------------------------------


class TestCorpusIndexPersistence:
    _SAMPLE = "def greet(name): return f'Hello {name}'"
    _OTHER = "class DataProcessor: pass"

    def test_add_directory_counts_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text(self._SAMPLE)
        (tmp_path / "b.py").write_text(self._OTHER)
        (tmp_path / "notes.txt").write_text("not python")
        cfg = FingerprintConfig(enabled=True)
        idx = CorpusIndex(cfg)
        count = idx.add_directory(tmp_path, glob="*.py")
        assert count == 2
        assert idx.size == 2

    def test_add_directory_max_files_respected(self, tmp_path: Path) -> None:
        for i in range(6):
            (tmp_path / f"f{i}.py").write_text(self._SAMPLE)
        cfg = FingerprintConfig(enabled=True)
        idx = CorpusIndex(cfg)
        count = idx.add_directory(tmp_path, glob="*.py", max_files=4)
        assert count == 4
        assert idx.size == 4

    def test_save_creates_file(self, tmp_path: Path) -> None:
        cfg = FingerprintConfig(enabled=True)
        idx = CorpusIndex(cfg)
        idx.add("sample.py", self._SAMPLE)
        out = tmp_path / "idx.json"
        idx.save(out)
        assert out.exists()
        raw = json.loads(out.read_text())
        assert raw["version"] == 1
        assert len(raw["entries"]) == 1

    def test_load_roundtrip_preserves_matches(self, tmp_path: Path) -> None:
        cfg = FingerprintConfig(enabled=True, threshold=0.5)
        idx = CorpusIndex(cfg)
        idx.add("sample.py", self._SAMPLE)
        out = tmp_path / "idx.json"
        idx.save(out)

        loaded = CorpusIndex.load(out, cfg)
        assert loaded.size == 1
        matches = loaded.query(self._SAMPLE)
        flagged = [m for m in matches if m.flagged]
        assert len(flagged) == 1
        assert flagged[0].source_label == "sample.py"

    def test_load_bad_version_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"version": 99, "entries": []}))
        cfg = FingerprintConfig(enabled=True)
        with pytest.raises(ValueError, match="version"):
            CorpusIndex.load(bad, cfg)
