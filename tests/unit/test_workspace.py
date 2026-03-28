"""Tests for bernstein.core.workspace — multi-repo workspace orchestration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.workspace import RepoConfig, Workspace

# ---------------------------------------------------------------------------
# Workspace.from_config
# ---------------------------------------------------------------------------


class TestFromConfig:
    """Tests for Workspace.from_config parsing."""

    def test_parses_basic_config(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {"name": "backend", "path": "./services/backend"},
                {"name": "frontend", "path": "./services/frontend"},
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        assert len(ws.repos) == 2
        assert ws.repos[0].name == "backend"
        assert ws.repos[0].path == Path("./services/backend")
        assert ws.repos[0].branch == "main"
        assert ws.repos[0].url is None
        assert ws.repos[1].name == "frontend"

    def test_parses_full_config_with_url_and_branch(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {
                    "name": "api",
                    "path": "./api",
                    "url": "git@github.com:org/api.git",
                    "branch": "develop",
                },
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].url == "git@github.com:org/api.git"
        assert ws.repos[0].branch == "develop"

    def test_root_is_resolved(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "x", "path": "./x"}]}
        ws = Workspace.from_config(config, root=tmp_path / "subdir")
        assert ws.root == (tmp_path / "subdir").resolve()

    def test_rejects_missing_repos_key(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"workspace\.repos must be a list"):
            Workspace.from_config({}, root=tmp_path)

    def test_rejects_non_list_repos(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"workspace\.repos must be a list"):
            Workspace.from_config({"repos": "invalid"}, root=tmp_path)

    def test_rejects_missing_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            Workspace.from_config({"repos": [{"path": "./x"}]}, root=tmp_path)

    def test_rejects_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty 'path'"):
            Workspace.from_config({"repos": [{"name": "x"}]}, root=tmp_path)

    def test_rejects_duplicate_names(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {"name": "dup", "path": "./a"},
                {"name": "dup", "path": "./b"},
            ]
        }
        with pytest.raises(ValueError, match="Duplicate repo name"):
            Workspace.from_config(config, root=tmp_path)


# ---------------------------------------------------------------------------
# Workspace.resolve_repo
# ---------------------------------------------------------------------------


class TestResolveRepo:
    """Tests for Workspace.resolve_repo path resolution."""

    def test_returns_correct_absolute_path(self, tmp_path: Path) -> None:
        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="svc", path=Path("./services/svc"))],
        )
        result = ws.resolve_repo("svc")
        assert result == (tmp_path / "services" / "svc").resolve()

    def test_handles_absolute_path(self, tmp_path: Path) -> None:
        abs_path = tmp_path / "absolute" / "repo"
        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="abs", path=abs_path)],
        )
        assert ws.resolve_repo("abs") == abs_path.resolve()

    def test_raises_keyerror_for_unknown(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path, repos=[])
        with pytest.raises(KeyError, match="Unknown repo"):
            ws.resolve_repo("nonexistent")


# ---------------------------------------------------------------------------
# Workspace.clone_missing
# ---------------------------------------------------------------------------


class TestCloneMissing:
    """Tests for Workspace.clone_missing git clone functionality."""

    @patch("bernstein.core.workspace.subprocess.run")
    def test_clones_missing_repo(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        ws = Workspace(
            root=tmp_path,
            repos=[
                RepoConfig(
                    name="new-repo",
                    path=Path("./repos/new-repo"),
                    url="https://github.com/org/new-repo.git",
                    branch="main",
                ),
            ],
        )
        cloned = ws.clone_missing()
        assert cloned == ["new-repo"]
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert args[1] == "clone"
        assert "--branch" in args
        assert "main" in args

    @patch("bernstein.core.workspace.subprocess.run")
    def test_skips_existing_repo(self, mock_run: MagicMock, tmp_path: Path) -> None:
        existing = tmp_path / "repos" / "existing"
        existing.mkdir(parents=True)
        ws = Workspace(
            root=tmp_path,
            repos=[
                RepoConfig(
                    name="existing",
                    path=Path("./repos/existing"),
                    url="https://github.com/org/existing.git",
                ),
            ],
        )
        cloned = ws.clone_missing()
        assert cloned == []
        mock_run.assert_not_called()

    def test_skips_repo_without_url(self, tmp_path: Path) -> None:
        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="local", path=Path("./local"))],
        )
        cloned = ws.clone_missing()
        assert cloned == []

    @patch("bernstein.core.workspace.subprocess.run")
    def test_handles_clone_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128, cmd=["git", "clone"], stderr="fatal: repo not found"
        )
        ws = Workspace(
            root=tmp_path,
            repos=[
                RepoConfig(
                    name="bad",
                    path=Path("./repos/bad"),
                    url="https://github.com/org/bad.git",
                ),
            ],
        )
        cloned = ws.clone_missing()
        assert cloned == []


# ---------------------------------------------------------------------------
# Workspace.status
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for Workspace.status git status retrieval."""

    def _make_git_repo(self, path: Path, branch: str = "main") -> None:
        """Create a minimal git repo at the given path."""
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-b", branch],
            cwd=str(path),
            capture_output=True,
            check=True,
        )
        # Make an initial commit so rev-parse works
        (path / "README.md").write_text("init")
        subprocess.run(
            ["git", "add", "."],
            cwd=str(path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com", "commit", "-m", "init"],
            cwd=str(path),
            capture_output=True,
            check=True,
        )

    def test_returns_status_for_valid_repos(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repos" / "svc"
        self._make_git_repo(repo_path, branch="main")

        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="svc", path=Path("./repos/svc"))],
        )
        result = ws.status()
        assert "svc" in result
        assert result["svc"].branch == "main"
        assert result["svc"].clean is True

    def test_detects_dirty_repo(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repos" / "dirty"
        self._make_git_repo(repo_path)
        (repo_path / "dirty.txt").write_text("uncommitted")

        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="dirty", path=Path("./repos/dirty"))],
        )
        result = ws.status()
        assert result["dirty"].clean is False

    def test_skips_nonexistent_repos(self, tmp_path: Path) -> None:
        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="ghost", path=Path("./repos/ghost"))],
        )
        result = ws.status()
        assert "ghost" not in result


# ---------------------------------------------------------------------------
# Workspace.validate
# ---------------------------------------------------------------------------


class TestValidate:
    """Tests for Workspace.validate health checks."""

    def test_no_issues_when_all_valid(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repos" / "valid"
        repo_path.mkdir(parents=True)
        (repo_path / ".git").mkdir()  # Fake .git dir

        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="valid", path=Path("./repos/valid"))],
        )
        issues = ws.validate()
        assert issues == []

    def test_detects_missing_repo(self, tmp_path: Path) -> None:
        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="missing", path=Path("./repos/missing"))],
        )
        issues = ws.validate()
        assert len(issues) == 1
        assert "does not exist" in issues[0]

    def test_detects_non_git_repo(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repos" / "not-git"
        repo_path.mkdir(parents=True)

        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="not-git", path=Path("./repos/not-git"))],
        )
        issues = ws.validate()
        assert len(issues) == 1
        assert "not a git repository" in issues[0]


# ---------------------------------------------------------------------------
# Task with repo field + spawner integration
# ---------------------------------------------------------------------------


class TestTaskRepoField:
    """Tests for the repo field on Task and its integration with the spawner."""

    def test_task_from_dict_includes_repo(self) -> None:
        from bernstein.core.models import Task

        raw = {
            "id": "abc123",
            "title": "Fix API",
            "description": "Fix the bug",
            "role": "backend",
            "repo": "api-service",
        }
        task = Task.from_dict(raw)
        assert task.repo == "api-service"

    def test_task_from_dict_repo_defaults_none(self) -> None:
        from bernstein.core.models import Task

        raw = {
            "id": "abc123",
            "title": "Fix API",
            "description": "Fix the bug",
            "role": "backend",
        }
        task = Task.from_dict(raw)
        assert task.repo is None

    def test_spawner_uses_workspace_repo_path(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Spawner should use the repo path as cwd when task has a repo field."""
        repo_path = tmp_path / "services" / "backend"
        repo_path.mkdir(parents=True)

        adapter = mock_adapter_factory(pid=100)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="backend", path=Path("./services/backend"))],
        )

        from bernstein.core.spawner import AgentSpawner

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, workspace=ws)

        task = make_task(id="T-100", role="backend")
        # Manually set repo since make_task doesn't support it
        task.repo = "backend"

        session = spawner.spawn_for_tasks([task])
        assert session.pid == 100

        # Verify the adapter was called with the repo path as workdir
        call_kwargs = adapter.spawn.call_args
        assert call_kwargs is not None
        spawn_workdir = call_kwargs.kwargs.get("workdir") or call_kwargs[1].get("workdir")
        assert spawn_workdir == repo_path.resolve()

    def test_spawner_falls_back_on_unknown_repo(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Spawner should fall back to workdir when repo name is not in workspace."""
        adapter = mock_adapter_factory(pid=101)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        ws = Workspace(root=tmp_path, repos=[])

        from bernstein.core.spawner import AgentSpawner

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, workspace=ws)

        task = make_task(id="T-101", role="backend")
        task.repo = "nonexistent"

        session = spawner.spawn_for_tasks([task])
        assert session.pid == 101

        # Should fall back to tmp_path
        call_kwargs = adapter.spawn.call_args
        spawn_workdir = call_kwargs.kwargs.get("workdir") or call_kwargs[1].get("workdir")
        assert spawn_workdir == tmp_path

    def test_spawner_ignores_repo_without_workspace(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        """Spawner with no workspace should ignore the repo field entirely."""
        adapter = mock_adapter_factory(pid=102)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        from bernstein.core.spawner import AgentSpawner

        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task(id="T-102", role="backend")
        task.repo = "some-repo"

        session = spawner.spawn_for_tasks([task])
        assert session.pid == 102


# ---------------------------------------------------------------------------
# Seed config integration
# ---------------------------------------------------------------------------


class TestSeedWorkspaceIntegration:
    """Tests for workspace parsing from bernstein.yaml via seed module."""

    def test_seed_parses_workspace_section(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            'goal: "Build the thing"\n'
            "workspace:\n"
            "  repos:\n"
            "    - name: backend\n"
            "      path: ./services/backend\n"
            "      url: git@github.com:org/backend.git\n"
            "    - name: frontend\n"
            "      path: ./services/frontend\n"
        )
        cfg = parse_seed(seed_file)
        assert cfg.workspace is not None
        assert len(cfg.workspace.repos) == 2
        assert cfg.workspace.repos[0].name == "backend"
        assert cfg.workspace.repos[0].url == "git@github.com:org/backend.git"
        assert cfg.workspace.repos[1].name == "frontend"
        assert cfg.workspace.repos[1].url is None

    def test_seed_without_workspace_returns_none(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Simple project"\n')
        cfg = parse_seed(seed_file)
        assert cfg.workspace is None

    def test_seed_rejects_invalid_workspace(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Test"\nworkspace: "invalid"\n')
        with pytest.raises(SeedError, match="workspace must be a mapping"):
            parse_seed(seed_file)


# ---------------------------------------------------------------------------
# CLI workspace commands
# ---------------------------------------------------------------------------


class TestWorkspaceCLI:
    """Tests for bernstein workspace CLI commands."""

    def test_workspace_validate_no_seed_file(self) -> None:
        from click.testing import CliRunner

        from bernstein.cli.main import workspace_validate

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(workspace_validate)
            assert result.exit_code == 0
            assert "No bernstein.yaml found" in result.output

    def test_workspace_clone_no_seed_file(self) -> None:
        from click.testing import CliRunner

        from bernstein.cli.main import workspace_clone

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(workspace_clone)
            assert result.exit_code == 0
            assert "No bernstein.yaml found" in result.output

    def test_workspace_validate_healthy(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.cli.main import workspace_validate

        # Create a repo with .git dir
        repo_path = tmp_path / "repos" / "svc"
        repo_path.mkdir(parents=True)
        (repo_path / ".git").mkdir()

        seed_content = f'goal: "Test"\nworkspace:\n  repos:\n    - name: svc\n      path: {repo_path}\n'
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(seed_content)

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Write the seed file in the isolated dir
            Path("bernstein.yaml").write_text(seed_content)
            result = runner.invoke(workspace_validate)
            assert result.exit_code == 0
            assert "healthy" in result.output
