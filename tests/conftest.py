"""Shared pytest fixtures for the bernstein test suite."""
from __future__ import annotations

import platform
import resource
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Memory guard: prevent any single pytest run from eating >4 GB RAM.
# This protects against runaway tests or recursive subprocess bombs.
# On macOS resource.RLIMIT_RSS is not enforced by the kernel, so we also
# install a periodic check via a pytest hook below.
# ---------------------------------------------------------------------------

_MAX_RSS_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB

if platform.system() != "Windows":
    try:
        _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (_MAX_RSS_BYTES, _hard))
    except (ValueError, AttributeError):
        pass  # RLIMIT_AS not available on all platforms


@pytest.fixture(autouse=True)
def _memory_guard():
    """Abort test if process RSS exceeds 4 GB (safety net)."""
    yield
    if platform.system() == "Darwin":
        # macOS: use resource.getrusage for RSS
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = usage.ru_maxrss  # macOS reports bytes
        if rss_bytes > _MAX_RSS_BYTES:
            print(
                f"\n\nFATAL: pytest RSS exceeded {_MAX_RSS_BYTES // (1024**3)} GB "
                f"(actual: {rss_bytes / (1024**3):.1f} GB). Aborting.\n",
                file=sys.stderr,
            )
            sys.exit(137)  # OOM-kill exit code

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
