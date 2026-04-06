"""Tests for atomic batch ingestion in Orchestrator.ingest_backlog().

Covers the 3-phase batch pattern: collect, POST /tasks/batch, move files.
All HTTP communication is mocked via httpx.MockTransport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from bernstein.core.models import OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_adapter() -> Any:
    """Minimal mock adapter that satisfies AgentSpawner."""
    from unittest.mock import MagicMock

    from bernstein.adapters.base import CLIAdapter

    adapter = MagicMock(spec=CLIAdapter)
    adapter.name.return_value = "mock"
    return adapter


def _build_orchestrator(
    tmp_path: Path,
    transport: httpx.MockTransport,
) -> Orchestrator:
    """Build an Orchestrator with a mock HTTP transport."""
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        heartbeat_timeout_s=120,
        max_tasks_per_agent=3,
        server_url="http://testserver",
    )
    adapter = _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


def _write_backlog_files(
    open_dir: Path,
    count: int,
    *,
    prefix: str = "task",
) -> list[Path]:
    """Write N valid markdown backlog files into *open_dir*."""
    files: list[Path] = []
    for i in range(count):
        p = open_dir / f"{prefix}-{i:03d}.md"
        p.write_text(
            f"# {prefix.title()} {i}\n\n"
            f"**Role:** backend\n"
            f"**Priority:** 2\n"
            f"**Scope:** medium\n"
            f"**Complexity:** low\n\n"
            f"Description for {prefix} {i}.\n",
            encoding="utf-8",
        )
        files.append(p)
    return files


def _setup_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create .sdd/backlog/open and .sdd/backlog/claimed directories."""
    open_dir = tmp_path / ".sdd" / "backlog" / "open"
    open_dir.mkdir(parents=True)
    claimed_dir = tmp_path / ".sdd" / "backlog" / "claimed"
    claimed_dir.mkdir(parents=True)
    return open_dir, claimed_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBatchIngestPostsAllAtOnce:
    """5 files should produce a single POST to /tasks/batch."""

    def test_single_batch_post(self, tmp_path: Path) -> None:
        open_dir, claimed_dir = _setup_dirs(tmp_path)
        _write_backlog_files(open_dir, 5)

        requests_log: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_log.append(request)
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tasks/batch":
                body = json.loads(request.content)
                assert len(body["tasks"]) == 5
                return httpx.Response(200, json={"created": 5})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)
        count = orch.ingest_backlog()

        assert count == 5
        batch_posts = [r for r in requests_log if r.url.path == "/tasks/batch"]
        assert len(batch_posts) == 1


class TestBatchIngestMovesAllOnSuccess:
    """All files should move to claimed/ after a successful batch POST."""

    def test_files_moved(self, tmp_path: Path) -> None:
        open_dir, claimed_dir = _setup_dirs(tmp_path)
        files = _write_backlog_files(open_dir, 3)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tasks/batch":
                return httpx.Response(200, json={"created": 3})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)
        count = orch.ingest_backlog()

        assert count == 3
        # All originals gone from open/
        for f in files:
            assert not f.exists(), f"{f.name} should have been moved"
        # All present in claimed/
        claimed_names = {p.name for p in claimed_dir.iterdir()}
        for f in files:
            assert f.name in claimed_names


class TestBatchIngestMovesNoneOnFailure:
    """If the batch POST returns 500, NO files should move."""

    def test_no_files_moved_on_500(self, tmp_path: Path) -> None:
        open_dir, claimed_dir = _setup_dirs(tmp_path)
        files = _write_backlog_files(open_dir, 4)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tasks/batch":
                return httpx.Response(500, json={"error": "internal"})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)
        count = orch.ingest_backlog()

        assert count == 0
        # All files should still be in open/
        for f in files:
            assert f.exists(), f"{f.name} should NOT have been moved"
        # Nothing in claimed/
        assert list(claimed_dir.iterdir()) == []


class TestBatchIngestFallbackOn404:
    """If the batch endpoint returns 404, fall back to one-by-one POSTs."""

    def test_fallback_to_individual_posts(self, tmp_path: Path) -> None:
        open_dir, claimed_dir = _setup_dirs(tmp_path)
        files = _write_backlog_files(open_dir, 3)

        individual_posts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tasks/batch":
                return httpx.Response(404, json={"error": "not found"})
            if request.method == "POST" and request.url.path == "/tasks":
                individual_posts.append(request)
                return httpx.Response(200, json={"id": "new", "status": "open"})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)
        count = orch.ingest_backlog()

        assert count == 3
        assert len(individual_posts) == 3
        # All files should be moved to claimed/
        for f in files:
            assert not f.exists()


class TestBatchIngestSkipsUnparseable:
    """Unparseable files should be moved to claimed/ individually."""

    def test_bad_files_moved(self, tmp_path: Path) -> None:
        open_dir, claimed_dir = _setup_dirs(tmp_path)

        # Write 2 valid + 1 unparseable file
        _write_backlog_files(open_dir, 2, prefix="good")
        bad_file = open_dir / "garbage.md"
        bad_file.write_text("", encoding="utf-8")  # empty = unparseable

        batch_payloads: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tasks/batch":
                body = json.loads(request.content)
                batch_payloads.append(body)
                return httpx.Response(200, json={"created": len(body["tasks"])})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)
        count = orch.ingest_backlog()

        # Only 2 valid files should have been batch-posted
        assert count == 2
        assert len(batch_payloads) == 1
        assert len(batch_payloads[0]["tasks"]) == 2

        # Bad file should be in claimed/
        assert not bad_file.exists()
        assert (claimed_dir / "garbage.md").exists()


class TestBatchIngestSkipsKnownTitles:
    """Files with titles already in _ingested_titles should be skipped."""

    def test_known_titles_skipped(self, tmp_path: Path) -> None:
        open_dir, claimed_dir = _setup_dirs(tmp_path)

        # Write 3 files; pre-populate _ingested_titles with title of first one
        files = _write_backlog_files(open_dir, 3, prefix="task")

        batch_payloads: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                # Return one task whose title matches "Task 0"
                return httpx.Response(200, json=[{"title": "Task 0"}])
            if request.method == "POST" and request.url.path == "/tasks/batch":
                body = json.loads(request.content)
                batch_payloads.append(body)
                return httpx.Response(200, json={"created": len(body["tasks"])})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)
        count = orch.ingest_backlog()

        # Task 0 should be skipped (known title), only 2 posted
        assert count == 2
        assert len(batch_payloads) == 1
        assert len(batch_payloads[0]["tasks"]) == 2

        # The skipped file (task-000) should be in claimed/ (dedup move)
        assert not files[0].exists()
        assert (claimed_dir / files[0].name).exists()
