"""Tests for the SQLite-backed codebase knowledge graph."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

import bernstein.core.knowledge_graph as knowledge_graph
import bernstein.core.semantic_graph as semantic_graph
from bernstein.cli.graph_cmd import graph_group
from bernstein.core.knowledge_graph import build_knowledge_graph, get_or_build_knowledge_graph, query_impact
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
