"""Tests for bernstein.core.workspace — multi-repo workspace coordinator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.workspace import Workspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_config(tmp_path: Path) -> dict:
    """A minimal workspace config dict referencing two repos."""
    return {
        "repos": [
            {
                "name": "backend",
                "path": str(tmp_path / "backend"),
                "url": "git@github.com:org/backend.git",
            },
            {
                "name": "frontend",
                "path": str(tmp_path / "frontend"),
                "url": "git@github.com:org/frontend.git",
                "branch": "develop",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Workspace.from_config
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_parses_repos(self, tmp_path: Path, simple_config: dict) -> None:
        ws = Workspace.from_config(simple_config, root=tmp_path)
        assert len(ws.repos) == 2
        assert ws.repos[0].name == "backend"
        assert ws.repos[1].name == "frontend"

    def test_resolves_relative_paths(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "core", "path": "./core"}]}
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].path == (tmp_path / "core").resolve()

    def test_preserves_absolute_paths(self, tmp_path: Path) -> None:
        abs_path = str(tmp_path / "absolute_repo")
        config = {"repos": [{"name": "abs", "path": abs_path}]}
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].path == Path(abs_path)

    def test_default_branch_is_main(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "repo", "path": "./repo"}]}
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].branch == "main"

    def test_custom_branch_parsed(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "fe", "path": "./fe", "branch": "develop"}]}
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].branch == "develop"

    def test_url_none_when_absent(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "local", "path": "./local"}]}
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].url is None

    def test_url_set_when_present(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "r", "path": "./r", "url": "git@github.com:o/r.git"}]}
        ws = Workspace.from_config(config, root=tmp_path)
        assert ws.repos[0].url == "git@github.com:o/r.git"

    def test_empty_repos_list(self, tmp_path: Path) -> None:
        ws = Workspace.from_config({"repos": []}, root=tmp_path)
        assert ws.repos == []

    def test_missing_repos_key(self, tmp_path: Path) -> None:
        ws = Workspace.from_config({}, root=tmp_path)
        assert ws.repos == []

    def test_missing_name_raises(self, tmp_path: Path) -> None:
        config = {"repos": [{"path": "./x"}]}
        with pytest.raises(ValueError, match="name"):
            Workspace.from_config(config, root=tmp_path)

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "x"}]}
        with pytest.raises(ValueError, match="path"):
            Workspace.from_config(config, root=tmp_path)

    def test_repos_not_list_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="list"):
            Workspace.from_config({"repos": "not-a-list"}, root=tmp_path)


# ---------------------------------------------------------------------------
# Workspace.resolve_repo
# ---------------------------------------------------------------------------


class TestResolveRepo:
    def test_returns_correct_path(self, tmp_path: Path, simple_config: dict) -> None:
        ws = Workspace.from_config(simple_config, root=tmp_path)
        path = ws.resolve_repo("backend")
        assert path == Path(simple_config["repos"][0]["path"])

    def test_returns_second_repo(self, tmp_path: Path, simple_config: dict) -> None:
        ws = Workspace.from_config(simple_config, root=tmp_path)
        path = ws.resolve_repo("frontend")
        assert path == Path(simple_config["repos"][1]["path"])

    def test_unknown_name_raises_key_error(self, tmp_path: Path, simple_config: dict) -> None:
        ws = Workspace.from_config(simple_config, root=tmp_path)
        with pytest.raises(KeyError, match="unknown"):
            ws.resolve_repo("unknown")


# ---------------------------------------------------------------------------
# Workspace.clone_missing
# ---------------------------------------------------------------------------


class TestCloneMissing:
    def test_clones_missing_repo(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {
                    "name": "myrepo",
                    "path": str(tmp_path / "myrepo"),
                    "url": "git@github.com:org/myrepo.git",
                },
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            cloned = ws.clone_missing()

        assert cloned == ["myrepo"]
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "git" in args
        assert "clone" in args
        assert "git@github.com:org/myrepo.git" in args

    def test_skips_existing_repo(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing"
        existing.mkdir()
        config = {
            "repos": [
                {"name": "existing", "path": str(existing), "url": "git@github.com:org/e.git"},
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        with patch("subprocess.run") as mock_run:
            cloned = ws.clone_missing()

        assert cloned == []
        mock_run.assert_not_called()

    def test_skips_repo_without_url(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {"name": "local", "path": str(tmp_path / "local")},
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        with patch("subprocess.run") as mock_run:
            cloned = ws.clone_missing()

        assert cloned == []
        mock_run.assert_not_called()

    def test_raises_on_git_failure(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {"name": "fail", "path": str(tmp_path / "fail"), "url": "git@github.com:org/fail.git"},
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stderr="fatal: repo not found")
            with pytest.raises(RuntimeError, match="git clone failed"):
                ws.clone_missing()

    def test_uses_configured_branch(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {
                    "name": "br",
                    "path": str(tmp_path / "br"),
                    "url": "git@github.com:org/br.git",
                    "branch": "release",
                },
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            ws.clone_missing()

        args = mock_run.call_args[0][0]
        assert "--branch" in args
        idx = args.index("--branch")
        assert args[idx + 1] == "release"


# ---------------------------------------------------------------------------
# Workspace.status
# ---------------------------------------------------------------------------


class TestWorkspaceStatus:
    def test_returns_status_for_each_repo(self, tmp_path: Path) -> None:
        config = {
            "repos": [
                {"name": "a", "path": str(tmp_path / "a")},
                {"name": "b", "path": str(tmp_path / "b")},
            ]
        }
        ws = Workspace.from_config(config, root=tmp_path)
        statuses = ws.status()
        assert set(statuses.keys()) == {"a", "b"}

    def test_missing_path_returns_error(self, tmp_path: Path) -> None:
        config = {"repos": [{"name": "gone", "path": str(tmp_path / "gone")}]}
        ws = Workspace.from_config(config, root=tmp_path)
        statuses = ws.status()
        assert "error" in statuses["gone"]
        assert "does not exist" in statuses["gone"]["error"]

    def test_status_keys_present_for_valid_repo(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config = {"repos": [{"name": "repo", "path": str(repo_dir)}]}
        ws = Workspace.from_config(config, root=tmp_path)

        def fake_run(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
            mock = MagicMock()
            if "rev-parse" in cmd:
                mock.returncode = 0
                mock.stdout = "main\n"
            elif "status" in cmd:
                mock.returncode = 0
                mock.stdout = ""
            elif "rev-list" in cmd:
                mock.returncode = 0
                mock.stdout = "0\t0\n"
            else:
                mock.returncode = 0
                mock.stdout = ""
            mock.stderr = ""
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            statuses = ws.status()

        info = statuses["repo"]
        assert info["branch"] == "main"
        assert info["state"] == "clean"
        assert info["ahead"] == "0"
        assert info["behind"] == "0"

    def test_dirty_state_detected(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config = {"repos": [{"name": "repo", "path": str(repo_dir)}]}
        ws = Workspace.from_config(config, root=tmp_path)

        def fake_run(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
            mock = MagicMock()
            if "rev-parse" in cmd:
                mock.returncode = 0
                mock.stdout = "feature/foo\n"
            elif "status" in cmd:
                mock.returncode = 0
                mock.stdout = " M somefile.py\n"
            elif "rev-list" in cmd:
                mock.returncode = 0
                mock.stdout = "2\t1\n"
            else:
                mock.returncode = 0
                mock.stdout = ""
            mock.stderr = ""
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            statuses = ws.status()

        info = statuses["repo"]
        assert info["state"] == "dirty"
        assert info["ahead"] == "2"
        assert info["behind"] == "1"


# ---------------------------------------------------------------------------
# Task.repo field + spawner workdir selection
# ---------------------------------------------------------------------------


class TestTaskRepoField:
    def test_task_repo_defaults_to_none(self, make_task) -> None:
        task = make_task()
        assert task.repo is None

    def test_task_repo_can_be_set(self) -> None:
        from bernstein.core.models import Task

        task = Task(id="T-1", title="x", description="y", role="backend", repo="backend")
        assert task.repo == "backend"

    def test_from_dict_deserializes_repo(self) -> None:
        from bernstein.core.models import Task

        raw = {
            "id": "T-1",
            "title": "t",
            "description": "d",
            "role": "backend",
            "repo": "frontend",
        }
        task = Task.from_dict(raw)
        assert task.repo == "frontend"

    def test_from_dict_repo_none_when_absent(self) -> None:
        from bernstein.core.models import Task

        raw = {"id": "T-1", "title": "t", "description": "d", "role": "backend"}
        task = Task.from_dict(raw)
        assert task.repo is None


class TestSpawnerRepoWorkdir:
    def test_uses_repo_workdir_when_task_has_repo(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner
        from bernstein.core.workspace import RepoConfig, Workspace

        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()
        ws = Workspace(
            root=tmp_path,
            repos=[RepoConfig(name="myrepo", path=repo_path)],
        )
        adapter = mock_adapter_factory(pid=55)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, workspace=ws)

        base_task = make_task(id="T-repo")
        task = base_task.__class__(
            id=base_task.id,
            title=base_task.title,
            description=base_task.description,
            role=base_task.role,
            repo="myrepo",
        )
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args
        assert call_kwargs.kwargs["workdir"] == repo_path

    def test_falls_back_to_workdir_when_no_workspace(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner

        adapter = mock_adapter_factory(pid=56)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        task = make_task(id="T-noworkspace")
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args
        assert call_kwargs.kwargs["workdir"] == tmp_path

    def test_falls_back_to_workdir_for_unknown_repo(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner
        from bernstein.core.workspace import Workspace

        ws = Workspace(root=tmp_path, repos=[])
        adapter = mock_adapter_factory(pid=57)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, workspace=ws)

        base_task = make_task(id="T-unknown")
        task = base_task.__class__(
            id=base_task.id,
            title=base_task.title,
            description=base_task.description,
            role=base_task.role,
            repo="nonexistent",
        )
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args
        assert call_kwargs.kwargs["workdir"] == tmp_path
