"""Regression tests for the two caches a RAG chunker fix has to get past.

Lives outside ``test_rag.py`` because that module is skipped wholesale
while the FTS5 indexer leaks memory; the builds here index one file.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.knowledge import rag
from bernstein.core.persistence import fingerprint as fingerprint_mod

if TYPE_CHECKING:
    from pathlib import Path


def _fake_chunker(marker: str) -> Any:
    def _chunk(source: str, rel_path: str) -> list[dict[str, object]]:
        return [
            {
                "file_path": rel_path,
                "line_start": 1,
                "line_end": 1,
                "symbols": marker,
                "content": source,
            }
        ]

    return _chunk


def _markers(chunks: list[dict[str, object]]) -> list[object]:
    return [chunk["symbols"] for chunk in chunks]


def _isolate_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give one test its own memo store, chunker, and source digests."""
    monkeypatch.setattr(rag, "_rag_memo_store", None)
    monkeypatch.setattr(rag, "_memoized_chunker", None)
    monkeypatch.setattr(
        fingerprint_mod,
        "_SOURCE_DIGEST_CACHE",
        dict(fingerprint_mod._SOURCE_DIGEST_CACHE),
    )


def _edit_chunker_source() -> None:
    """Simulate an edit to rag.py.

    Shifting its source digest is what a real edit does on the next
    interpreter start.
    """
    fingerprint_mod._SOURCE_DIGEST_CACHE[rag.__name__] = hashlib.sha256(b"edited").digest()


class TestChunkerMemoInvalidation:
    """A chunker fix must not be masked by memoised chunks.

    ``_chunk_for_memo`` only dispatches, so its own body is identical
    before and after any edit to ``_extract_python_chunks`` or
    ``_line_chunks``.  Unless the module owning them is a declared memo
    dependency, every file whose bytes did not change keeps serving
    chunks shaped by the old chunker.
    """

    @pytest.mark.parametrize(
        ("is_python", "chunker_attr"),
        [(True, "_extract_python_chunks"), (False, "_line_chunks")],
    )
    def test_chunker_change_invalidates_cached_entry_for_unchanged_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        is_python: bool,
        chunker_attr: str,
    ) -> None:
        _isolate_caches(monkeypatch)

        source = "def run() -> int:\n    return 1\n"
        rel_path = "pkg/mod.py"

        monkeypatch.setattr(rag, chunker_attr, _fake_chunker("old"))
        first = rag._chunk_with_memo(tmp_path, source, rel_path, is_python=is_python)
        assert _markers(first) == ["old"]

        # Same bytes, same chunker: the second call must be a cache hit.
        monkeypatch.setattr(rag, chunker_attr, _fake_chunker("not-called"))
        cached = rag._chunk_with_memo(tmp_path, source, rel_path, is_python=is_python)
        assert _markers(cached) == ["old"]

        _edit_chunker_source()

        monkeypatch.setattr(rag, chunker_attr, _fake_chunker("new"))
        refreshed = rag._chunk_with_memo(tmp_path, source, rel_path, is_python=is_python)
        assert _markers(refreshed) == ["new"], "stale chunks served after the chunker changed"

    def test_chunker_module_is_a_declared_memo_dependency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rag, "_rag_memo_store", None)
        monkeypatch.setattr(rag, "_memoized_chunker", None)

        chunker = rag._get_memoized_chunker(tmp_path)

        assert rag in chunker.__memo_depends_on__


class TestIndexChunkerRevision:
    """A chunker fix must also get past the incremental index.

    ``_build_inner`` re-reads a file only when its mtime moved, so for an
    unchanged file the chunker is never called and the memo key never
    consulted.  Rows written by the previous chunker stay in FTS until
    the revision recorded beside them stops matching.
    """

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "mod.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
        return project

    @staticmethod
    def _indexed_symbols(indexer: rag.CodebaseIndexer) -> list[str]:
        conn = sqlite3.connect(str(indexer.db_path))
        try:
            return sorted(row[0] for row in conn.execute("SELECT symbols FROM chunks"))
        finally:
            conn.close()

    def test_chunker_change_reindexes_files_whose_bytes_did_not_move(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_caches(monkeypatch)
        project = self._project(tmp_path)

        monkeypatch.setattr(rag, "_extract_python_chunks", _fake_chunker("old"))
        indexer = rag.CodebaseIndexer(project)
        assert indexer.build() == 1
        assert self._indexed_symbols(indexer) == ["old"]

        # Nothing moved on disk, so the mtime gate skips the file.
        assert indexer.build() == 0

        # A chunker edit does not touch any indexed file's mtime.
        _edit_chunker_source()
        monkeypatch.setattr(rag, "_extract_python_chunks", _fake_chunker("new"))

        assert indexer.build() == 1, "chunker change left the index untouched"
        assert self._indexed_symbols(indexer) == ["new"], "stale FTS rows survived the chunker change"

        # The new revision is recorded, so the sweep does not repeat.
        assert indexer.build() == 0

    def test_index_predating_the_revision_column_is_rebuilt_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolate_caches(monkeypatch)
        project = self._project(tmp_path)

        monkeypatch.setattr(rag, "_extract_python_chunks", _fake_chunker("old"))
        indexer = rag.CodebaseIndexer(project)
        assert indexer.build() == 1

        # An index written before the revision was tracked has no row.
        conn = sqlite3.connect(str(indexer.db_path))
        try:
            conn.execute("DELETE FROM index_meta WHERE key = 'chunker_revision'")
            conn.commit()
        finally:
            conn.close()

        # The file is re-read because the index cannot prove which chunker
        # shaped it.  The chunker source itself did not move, so the memo
        # layer still answers - the two gates are independent, and only the
        # one with evidence of a change fires.
        monkeypatch.setattr(rag, "_extract_python_chunks", _fake_chunker("not-called"))
        assert indexer.build() == 1
        assert self._indexed_symbols(indexer) == ["old"]
        assert indexer.build() == 0

    def test_revision_matches_the_memo_dependency_digest(self) -> None:
        assert rag._chunker_revision() == fingerprint_mod.code_digest(rag).hex()
