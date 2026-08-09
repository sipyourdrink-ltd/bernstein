"""Tests for the SQLite-backed codebase knowledge graph."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from bernstein.cli.graph_cmd import graph_group
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

import bernstein.core.knowledge.ast_symbol_graph as semantic_graph
import bernstein.core.knowledge.knowledge_graph as knowledge_graph
from bernstein.core.knowledge.knowledge_graph import (
    build_knowledge_graph,
    get_or_build_knowledge_graph,
    query_impact,
)
from bernstein.core.persistence import fingerprint as fingerprint_mod
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _patch_ls_files(monkeypatch: pytest.MonkeyPatch, files: list[str]) -> None:
    def _fake_ls_files(_workdir: Path) -> list[str]:
        return files

    monkeypatch.setattr(knowledge_graph, "_git_ls_files", _fake_ls_files)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", _fake_ls_files)


@pytest_asyncio.fixture()
async def client(tmp_path: Path) -> AsyncGenerator[AsyncClient, None]:
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=jsonl_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


class TestKnowledgeGraphBuild:
    def test_builds_sqlite_graph_and_queries_transitive_impact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(tmp_path / "src" / "pkg" / "__init__.py", "")
        _write(tmp_path / "src" / "pkg" / "helpers.py", "def helper() -> int:\n    return 1\n")
        _write(
            tmp_path / "src" / "pkg" / "service.py",
            "from pkg.helpers import helper\n\ndef run() -> int:\n    return helper()\n",
        )
        _write(
            tmp_path / "src" / "pkg" / "controller.py",
            "from pkg.service import run\n\ndef handle() -> int:\n    return run()\n",
        )

        files = ["src/pkg/helpers.py", "src/pkg/service.py", "src/pkg/controller.py"]
        _patch_ls_files(monkeypatch, files)

        db_path = build_knowledge_graph(tmp_path)
        impact = query_impact(tmp_path, "helpers.py")

        assert db_path == tmp_path / ".sdd" / "index" / "knowledge_graph.db"
        assert db_path.exists()
        assert impact.matched_files == ["src/pkg/helpers.py"]
        assert impact.impacted_files == ["src/pkg/controller.py", "src/pkg/service.py"]

    def test_reuses_fresh_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "src" / "pkg" / "__init__.py", "")
        _write(tmp_path / "src" / "pkg" / "only.py", "def run() -> int:\n    return 1\n")

        files = ["src/pkg/only.py"]
        _patch_ls_files(monkeypatch, files)
        db_path = build_knowledge_graph(tmp_path)

        def _should_not_build(_workdir: Path) -> Path:
            raise AssertionError("cache should be reused")

        monkeypatch.setattr(knowledge_graph, "build_knowledge_graph", _should_not_build)
        reused_path = get_or_build_knowledge_graph(tmp_path)
        assert reused_path == db_path


class TestSymbolMemoInvalidation:
    """A parser fix must not be masked by memoised symbol data.

    ``_extract_symbols_for_memo`` is a one-line shim, so its own body is
    identical before and after any edit to ``ast_symbol_graph``.  Unless
    the extractor module is a declared memo dependency, every file whose
    bytes did not change keeps serving symbols built by the old parser.
    """

    @staticmethod
    def _fake_parser(marker: str) -> Any:
        def _parse(_filepath: Path, rel_path: str) -> semantic_graph.FileSymbols:
            return semantic_graph.FileSymbols(path=rel_path, imports={"marker": marker})

        return _parse

    def test_extractor_change_invalidates_cached_entry_for_unchanged_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force a fresh memo store rooted under tmp_path; monkeypatch restores
        # the process-wide extractor afterwards.
        monkeypatch.setattr(knowledge_graph, "_kg_memo_store", None)
        monkeypatch.setattr(knowledge_graph, "_memoized_extract", None)
        monkeypatch.setattr(
            fingerprint_mod,
            "_SOURCE_DIGEST_CACHE",
            dict(fingerprint_mod._SOURCE_DIGEST_CACHE),
        )

        source = tmp_path / "pkg" / "mod.py"
        _write(source, "def run() -> int:\n    return 1\n")
        rel_path = "pkg/mod.py"

        monkeypatch.setattr(knowledge_graph, "parse_file_symbols", self._fake_parser("old"))
        first = knowledge_graph._parse_file_symbols_memoized(tmp_path, source, rel_path)
        assert first is not None
        assert first.imports == {"marker": "old"}

        # Same bytes, same extractor: the second call must be a cache hit.
        monkeypatch.setattr(knowledge_graph, "parse_file_symbols", self._fake_parser("not-called"))
        cached = knowledge_graph._parse_file_symbols_memoized(tmp_path, source, rel_path)
        assert cached is not None
        assert cached.imports == {"marker": "old"}

        # Simulate an edit to ast_symbol_graph.py by shifting its source
        # digest, which is what a real edit does.  The cached freshness token
        # is kept so the substitute digest survives the staleness check
        # instead of being immediately recomputed from the unedited file.
        cache = fingerprint_mod._SOURCE_DIGEST_CACHE
        token = cache[semantic_graph.__name__][0]
        cache[semantic_graph.__name__] = (token, hashlib.sha256(b"edited").digest())

        monkeypatch.setattr(knowledge_graph, "parse_file_symbols", self._fake_parser("new"))
        refreshed = knowledge_graph._parse_file_symbols_memoized(tmp_path, source, rel_path)
        assert refreshed is not None
        assert refreshed.imports == {"marker": "new"}, "stale symbols served after the extractor changed"

    def test_extractor_module_is_a_declared_memo_dependency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(knowledge_graph, "_kg_memo_store", None)
        monkeypatch.setattr(knowledge_graph, "_memoized_extract", None)

        extractor = knowledge_graph._get_memoized_extract(tmp_path)

        assert semantic_graph in extractor.__memo_depends_on__


class TestKnowledgeGraphIntegrations:
    @pytest.mark.asyncio
    async def test_route_returns_impacted_files(
        self,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(tmp_path / "src" / "pkg" / "__init__.py", "")
        _write(tmp_path / "src" / "pkg" / "helpers.py", "def helper() -> int:\n    return 1\n")
        _write(
            tmp_path / "src" / "pkg" / "service.py",
            "from pkg.helpers import helper\n\ndef run() -> int:\n    return helper()\n",
        )

        _patch_ls_files(monkeypatch, ["src/pkg/helpers.py", "src/pkg/service.py"])

        response = await client.get("/graph/impact", params={"file": "helpers.py"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["matched_files"] == ["src/pkg/helpers.py"]
        assert payload["impacted_files"] == ["src/pkg/service.py"]

    def test_cli_prints_impacted_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "src" / "pkg" / "__init__.py", "")
        _write(tmp_path / "src" / "pkg" / "helpers.py", "def helper() -> int:\n    return 1\n")
        _write(
            tmp_path / "src" / "pkg" / "service.py",
            "from pkg.helpers import helper\n\ndef run() -> int:\n    return helper()\n",
        )

        _patch_ls_files(monkeypatch, ["src/pkg/helpers.py", "src/pkg/service.py"])
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(graph_group, ["impact", "helpers.py"])

        assert result.exit_code == 0, result.output
        assert "src/pkg/service.py" in result.output


class TestFreshnessWindowRespectsExtractorRevision:
    """A recent graph built by a different extractor is still stale.

    ``depends_on`` invalidates memo entries only when the memoised extractor
    is called, and the age fast path returns before that happens. Without a
    stored revision to compare, an extractor fix is masked for the whole
    window while ``query_impact`` and ``export_graph_summary`` keep serving
    symbols the fix was meant to correct.
    """

    def test_graph_within_the_age_window_rebuilds_when_the_extractor_moves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(tmp_path / "pkg" / "mod.py", "def run() -> int:\n    return 1\n")
        monkeypatch.setattr(knowledge_graph, "_git_ls_files", lambda _workdir: ["pkg/mod.py"])

        db_path = knowledge_graph.build_knowledge_graph(tmp_path)
        connection = knowledge_graph._connect(db_path)
        try:
            built_at = knowledge_graph._read_built_at(connection)
            revision = knowledge_graph._read_metadata(connection, "extractor_revision")
        finally:
            connection.close()
        assert built_at is not None
        assert revision == knowledge_graph._extractor_revision()

        # Unchanged extractor, well inside the window: no rebuild.
        calls: list[int] = []
        real_build = knowledge_graph.build_knowledge_graph
        monkeypatch.setattr(
            knowledge_graph,
            "build_knowledge_graph",
            lambda workdir: (calls.append(1), real_build(workdir))[1],
        )
        knowledge_graph.get_or_build_knowledge_graph(tmp_path, max_age_minutes=30)
        assert calls == [], "a current graph inside the window must not be rebuilt"

        # Same graph, same age - but a different extractor revision.
        monkeypatch.setattr(knowledge_graph, "_extractor_revision", lambda: "0" * 64)
        knowledge_graph.get_or_build_knowledge_graph(tmp_path, max_age_minutes=30)
        assert calls == [1], "a graph built by a different extractor must be rebuilt"

    def test_a_database_without_the_key_rebuilds_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Databases written before this key existed must not be trusted."""
        _write(tmp_path / "pkg" / "mod.py", "def run() -> int:\n    return 1\n")
        monkeypatch.setattr(knowledge_graph, "_git_ls_files", lambda _workdir: ["pkg/mod.py"])

        db_path = knowledge_graph.build_knowledge_graph(tmp_path)
        connection = knowledge_graph._connect(db_path)
        try:
            with connection:
                connection.execute("DELETE FROM metadata WHERE key = 'extractor_revision'")
        finally:
            connection.close()

        calls: list[int] = []
        real_build = knowledge_graph.build_knowledge_graph
        monkeypatch.setattr(
            knowledge_graph,
            "build_knowledge_graph",
            lambda workdir: (calls.append(1), real_build(workdir))[1],
        )
        knowledge_graph.get_or_build_knowledge_graph(tmp_path, max_age_minutes=30)
        assert calls == [1], "a graph with no recorded extractor revision must be rebuilt"

    def test_a_revision_change_during_the_build_is_not_stamped_onto_old_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The recorded revision must be the one parsing started under.

        Read after the fact it would name whatever the extractor file says
        once the build finishes, while the nodes and edges came from the
        module already resident in memory. That build would then match on the
        next call and its stale output would be trusted indefinitely.
        """
        _write(tmp_path / "pkg" / "mod.py", "def run() -> int:\n    return 1\n")
        monkeypatch.setattr(knowledge_graph, "_git_ls_files", lambda _workdir: ["pkg/mod.py"])

        before = "a" * 64
        after = "b" * 64
        revisions = iter([before, after, after, after])
        monkeypatch.setattr(knowledge_graph, "_extractor_revision", lambda: next(revisions))

        db_path = knowledge_graph.build_knowledge_graph(tmp_path)
        connection = knowledge_graph._connect(db_path)
        try:
            recorded = knowledge_graph._read_metadata(connection, "extractor_revision")
        finally:
            connection.close()

        assert recorded == before, (
            "the build recorded a revision it did not parse under; the graph "
            "would then be trusted as current output of the newer extractor"
        )
