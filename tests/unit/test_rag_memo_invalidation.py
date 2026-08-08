"""Memo-key regression tests for the RAG chunker.

Lives outside ``test_rag.py`` because that module is skipped wholesale
while the FTS5 indexer leaks memory; nothing here builds an index.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.knowledge import rag
from bernstein.core.persistence import fingerprint as fingerprint_mod

if TYPE_CHECKING:
    from pathlib import Path


class TestChunkerMemoInvalidation:
    """A chunker fix must not be masked by memoised chunks.

    ``_chunk_for_memo`` only dispatches, so its own body is identical
    before and after any edit to ``_extract_python_chunks`` or
    ``_line_chunks``.  Unless the module owning them is a declared memo
    dependency, every file whose bytes did not change keeps serving
    chunks shaped by the old chunker.
    """

    @staticmethod
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

    @staticmethod
    def _markers(chunks: list[dict[str, object]]) -> list[object]:
        return [chunk["symbols"] for chunk in chunks]

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
        # Force a fresh memo store rooted under tmp_path; monkeypatch restores
        # the process-wide chunker afterwards.
        monkeypatch.setattr(rag, "_rag_memo_store", None)
        monkeypatch.setattr(rag, "_memoized_chunker", None)
        monkeypatch.setattr(
            fingerprint_mod,
            "_SOURCE_DIGEST_CACHE",
            dict(fingerprint_mod._SOURCE_DIGEST_CACHE),
        )

        source = "def run() -> int:\n    return 1\n"
        rel_path = "pkg/mod.py"

        monkeypatch.setattr(rag, chunker_attr, self._fake_chunker("old"))
        first = rag._chunk_with_memo(tmp_path, source, rel_path, is_python=is_python)
        assert self._markers(first) == ["old"]

        # Same bytes, same chunker: the second call must be a cache hit.
        monkeypatch.setattr(rag, chunker_attr, self._fake_chunker("not-called"))
        cached = rag._chunk_with_memo(tmp_path, source, rel_path, is_python=is_python)
        assert self._markers(cached) == ["old"]

        # Simulate an edit to rag.py.  Shifting its source digest is what a
        # real edit does on the next interpreter start.
        fingerprint_mod._SOURCE_DIGEST_CACHE[rag.__name__] = hashlib.sha256(b"edited").digest()

        monkeypatch.setattr(rag, chunker_attr, self._fake_chunker("new"))
        refreshed = rag._chunk_with_memo(tmp_path, source, rel_path, is_python=is_python)
        assert self._markers(refreshed) == ["new"], "stale chunks served after the chunker changed"

    def test_chunker_module_is_a_declared_memo_dependency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rag, "_rag_memo_store", None)
        monkeypatch.setattr(rag, "_memoized_chunker", None)

        chunker = rag._get_memoized_chunker(tmp_path)

        assert rag in chunker.__memo_depends_on__
