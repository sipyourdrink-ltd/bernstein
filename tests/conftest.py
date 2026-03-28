"""Shared pytest fixtures for the bernstein test suite."""

from __future__ import annotations

import gc
import platform
import resource
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Memory guard: prevent any single pytest run from eating >2 GB RAM.
# ---------------------------------------------------------------------------

_MAX_RSS_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

if platform.system() != "Windows":
    try:
        _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (_MAX_RSS_BYTES, _hard))
    except (ValueError, AttributeError):
        pass  # RLIMIT_AS not available on all platforms


@pytest.fixture(autouse=True)
def _memory_guard():
    """Force GC before and after every test; abort if RSS exceeds limit."""
    yield
    # Aggressive garbage collection to prevent accumulation
    gc.collect()
    if platform.system() == "Darwin":
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = usage.ru_maxrss  # macOS reports bytes
        if rss_bytes > _MAX_RSS_BYTES:
            print(
                f"\n\nFATAL: pytest RSS exceeded {_MAX_RSS_BYTES // (1024**3)} GB "
                f"(actual: {rss_bytes / (1024**3):.1f} GB). Aborting.\n",
                file=sys.stderr,
            )
            sys.exit(137)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Aggressively clear references that pytest holds onto after each test."""
    # Clear funcargs/fixtures that may hold large mock objects or tmp_path data
    if hasattr(item, "funcargs"):
        item.funcargs.clear()
    # Clear report sections (captured stdout/stderr per test)
    if hasattr(item, "_report_sections"):
        item._report_sections.clear()
    gc.collect()


from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import (
    Complexity,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)


@pytest.fixture
def make_task():
    """Factory fixture for Task objects with sensible defaults.

    Supports all common Task fields; tests override only what they care about.
    """

    def _factory(
        *,
        id: str = "T-001",
        role: str = "backend",
        title: str = "Implement feature",
        description: str = "Write the code.",
        scope: Scope = Scope.MEDIUM,
        complexity: Complexity = Complexity.MEDIUM,
        status: TaskStatus = TaskStatus.OPEN,
        task_type: TaskType = TaskType.STANDARD,
        priority: int = 2,
        owned_files: list[str] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> Task:
        return Task(
            id=id,
            title=title,
            description=description,
            role=role,
            scope=scope,
            complexity=complexity,
            status=status,
            task_type=task_type,
            priority=priority,
            owned_files=owned_files or [],
            mcp_servers=mcp_servers or [],
        )

    return _factory


@pytest.fixture
def mock_adapter_factory():
    """Factory fixture for CLIAdapter mocks with configurable PID."""

    def _factory(pid: int = 42) -> CLIAdapter:
        adapter = MagicMock(spec=CLIAdapter)
        adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
        adapter.is_alive.return_value = True
        adapter.kill.return_value = None
        adapter.name.return_value = "MockCLI"
        return adapter

    return _factory


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    """Temporary .sdd directory with standard subdirectories pre-created."""
    sdd = tmp_path / ".sdd"
    (sdd / "backlog" / "open").mkdir(parents=True)
    (sdd / "backlog" / "done").mkdir(parents=True)
    (sdd / "runtime").mkdir(parents=True)
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "upgrades").mkdir(parents=True)
    return sdd
