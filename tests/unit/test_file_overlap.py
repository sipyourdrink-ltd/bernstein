"""Tests for CRITICAL-007: file overlap detection and inferred affected paths.

Covers ``infer_affected_paths``, ``_get_active_agent_files``, and the
updated ``check_file_overlap`` that uses inferred paths.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.models import AgentSession, ModelConfig
from bernstein.core.task_lifecycle import (
    _get_active_agent_files,
    check_file_overlap,
    infer_affected_paths,
)

# ---------------------------------------------------------------------------
# infer_affected_paths tests
# ---------------------------------------------------------------------------


class TestInferAffectedPaths:
    """Unit tests for infer_affected_paths."""

    def test_explicit_src_path(self, make_task: Any) -> None:
        """Finds fully qualified src/bernstein paths in description."""
        task = make_task(
            description="Modify src/bernstein/core/orchestrator.py to fix spawner bug.",
        )
        paths = infer_affected_paths(task)
        assert "src/bernstein/core/orchestrator.py" in paths

    def test_explicit_test_path(self, make_task: Any) -> None:
        """Finds paths under tests/unit/ in description."""
        task = make_task(
            description="Update tests/unit/test_spawner.py to add coverage.",
        )
        paths = infer_affected_paths(task)
        assert "tests/unit/test_spawner.py" in paths

    def test_explicit_integration_test_path(self, make_task: Any) -> None:
        """Finds paths under tests/integration/ in description."""
        task = make_task(
            description="Fix tests/integration/test_e2e.py flaky assertion.",
        )
        paths = infer_affected_paths(task)
        assert "tests/integration/test_e2e.py" in paths

    def test_multiple_paths(self, make_task: Any) -> None:
        """Extracts multiple paths from a single description."""
        task = make_task(
            description=(
                "Refactor src/bernstein/core/models.py and "
                "src/bernstein/core/spawner.py for consistency."
            ),
        )
        paths = infer_affected_paths(task)
        assert "src/bernstein/core/models.py" in paths
        assert "src/bernstein/core/spawner.py" in paths

    def test_paths_in_title(self, make_task: Any) -> None:
        """Extracts paths from the task title, not just description."""
        task = make_task(
            title="Fix src/bernstein/core/tick_pipeline.py timeout",
            description="The tick pipeline has a race condition.",
        )
        paths = infer_affected_paths(task)
        assert "src/bernstein/core/tick_pipeline.py" in paths

    def test_bare_module_name_resolved(self, make_task: Any) -> None:
        """Bare module names like 'orchestrator.py' are resolved via rglob."""
        task = make_task(
            description="Refactor orchestrator.py to reduce complexity.",
        )
        # Mock Path.rglob to return a fake match
        fake_path = Path("src/bernstein/core/orchestrator.py")
        with patch("pathlib.Path.rglob", return_value=iter([fake_path])):
            paths = infer_affected_paths(task)
        assert "src/bernstein/core/orchestrator.py" in paths

    def test_bare_module_skipped_if_already_qualified(self, make_task: Any) -> None:
        """Bare module names are not re-resolved if a full path was already found."""
        task = make_task(
            description="Fix src/bernstein/core/orchestrator.py imports in orchestrator.py.",
        )
        with patch("pathlib.Path.rglob") as mock_rglob:
            paths = infer_affected_paths(task)
        # "orchestrator.py" should not trigger rglob since a full path exists
        for call in mock_rglob.call_args_list:
            assert call.args[0] != "orchestrator.py", (
                "rglob should not be called for orchestrator.py when full path exists"
            )
        assert "src/bernstein/core/orchestrator.py" in paths

    def test_no_paths_returns_empty(self, make_task: Any) -> None:
        """Returns empty set when no Python file paths are mentioned."""
        task = make_task(
            title="Improve documentation",
            description="Write better docstrings throughout the project.",
        )
        with patch("pathlib.Path.rglob", return_value=iter([])):
            paths = infer_affected_paths(task)
        assert paths == set()

    def test_non_python_files_ignored(self, make_task: Any) -> None:
        """Non-.py files are not extracted."""
        task = make_task(
            description="Update README.md and src/bernstein/config.yaml.",
        )
        with patch("pathlib.Path.rglob", return_value=iter([])):
            paths = infer_affected_paths(task)
        # .md and .yaml should not appear
        assert not any(p.endswith(".md") for p in paths)
        assert not any(p.endswith(".yaml") for p in paths)


# ---------------------------------------------------------------------------
# _get_active_agent_files tests
# ---------------------------------------------------------------------------


class TestGetActiveAgentFiles:
    """Unit tests for _get_active_agent_files."""

    def test_returns_changed_files_from_worktree(self) -> None:
        """Files from git diff in active agent worktrees are included."""
        session = AgentSession(
            id="A-1",
            role="backend",
            task_ids=["T-1"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="alive",
        )
        spawner = MagicMock()
        spawner.get_worktree_path.return_value = Path("/worktrees/A-1")
        orch = SimpleNamespace(
            _agents={"A-1": session},
            _spawner=spawner,
            _file_ownership={},
        )

        with patch(
            "bernstein.core.task_lifecycle._get_changed_files_in_worktree",
            return_value=["src/bernstein/core/models.py", "src/bernstein/core/spawner.py"],
        ):
            files = _get_active_agent_files(orch)

        assert "src/bernstein/core/models.py" in files
        assert "src/bernstein/core/spawner.py" in files

    def test_includes_file_ownership_entries(self) -> None:
        """Statically declared file ownership is included even without worktree changes."""
        session = AgentSession(
            id="A-1",
            role="backend",
            task_ids=["T-1"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="alive",
        )
        spawner = MagicMock()
        spawner.get_worktree_path.return_value = None
        orch = SimpleNamespace(
            _agents={"A-1": session},
            _spawner=spawner,
            _file_ownership={"src/bernstein/core/auth.py": "A-1"},
        )

        files = _get_active_agent_files(orch)
        assert "src/bernstein/core/auth.py" in files

    def test_dead_agents_excluded(self) -> None:
        """Dead agents' files are not included."""
        session = AgentSession(
            id="A-dead",
            role="backend",
            task_ids=["T-1"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="dead",
        )
        spawner = MagicMock()
        orch = SimpleNamespace(
            _agents={"A-dead": session},
            _spawner=spawner,
            _file_ownership={"src/foo.py": "A-dead"},
        )

        files = _get_active_agent_files(orch)
        assert files == set()

    def test_no_spawner_graceful(self) -> None:
        """Works gracefully when spawner is None."""
        session = AgentSession(
            id="A-1",
            role="backend",
            task_ids=["T-1"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="alive",
        )
        orch = SimpleNamespace(
            _agents={"A-1": session},
            _spawner=None,
            _file_ownership={"src/foo.py": "A-1"},
        )

        files = _get_active_agent_files(orch)
        assert "src/foo.py" in files


# ---------------------------------------------------------------------------
# check_file_overlap with inferred paths
# ---------------------------------------------------------------------------


class TestCheckFileOverlapWithInferredPaths:
    """Tests that check_file_overlap uses inferred paths from task content."""

    def test_inferred_path_triggers_overlap(self, make_task: Any) -> None:
        """A task mentioning a file in its description conflicts with an owned file."""
        task = make_task(
            id="T-new",
            description="Fix src/bernstein/core/orchestrator.py race condition.",
            owned_files=[],
        )
        live_agent = AgentSession(
            id="A-owner",
            role="backend",
            task_ids=["T-old"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="alive",
        )
        file_ownership = {"src/bernstein/core/orchestrator.py": "A-owner"}
        agents = {"A-owner": live_agent}

        assert check_file_overlap([task], file_ownership, agents) is True

    def test_no_overlap_when_different_files(self, make_task: Any) -> None:
        """No conflict when inferred paths don't overlap with owned files."""
        task = make_task(
            id="T-new",
            description="Fix src/bernstein/core/metrics.py bug.",
            owned_files=[],
        )
        live_agent = AgentSession(
            id="A-owner",
            role="backend",
            task_ids=["T-old"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="alive",
        )
        file_ownership = {"src/bernstein/core/orchestrator.py": "A-owner"}
        agents = {"A-owner": live_agent}

        assert check_file_overlap([task], file_ownership, agents) is False

    def test_dead_agent_does_not_block(self, make_task: Any) -> None:
        """Inferred overlap with a dead agent does not block."""
        task = make_task(
            id="T-new",
            description="Refactor src/bernstein/core/orchestrator.py.",
            owned_files=[],
        )
        dead_agent = AgentSession(
            id="A-dead",
            role="backend",
            task_ids=["T-old"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="dead",
        )
        file_ownership = {"src/bernstein/core/orchestrator.py": "A-dead"}
        agents = {"A-dead": dead_agent}

        assert check_file_overlap([task], file_ownership, agents) is False

    def test_explicit_owned_files_still_checked(self, make_task: Any) -> None:
        """Original owned_files logic still works alongside inference."""
        task = make_task(
            id="T-new",
            owned_files=["src/auth.py"],
        )
        live_agent = AgentSession(
            id="A-owner",
            role="backend",
            task_ids=["T-old"],
            model_config=ModelConfig(model="sonnet", effort="high"),
            status="alive",
        )
        file_ownership = {"src/auth.py": "A-owner"}
        agents = {"A-owner": live_agent}

        assert check_file_overlap([task], file_ownership, agents) is True
