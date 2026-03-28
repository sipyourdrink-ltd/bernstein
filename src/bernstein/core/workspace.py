"""Multi-repo workspace orchestration.

Manages a collection of git repositories as a single workspace.
Tasks can target specific repos, and the spawner routes agents to
the correct working directory.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoConfig:
    """Configuration for a single repository in the workspace.

    Attributes:
        name: Short identifier for the repo (e.g. "backend").
        path: Relative or absolute path to the repo root.
        url: Optional git clone URL.
        branch: Default branch name.
    """

    name: str
    path: Path
    url: str | None = None
    branch: str = "main"


@dataclass(frozen=True)
class RepoStatus:
    """Git status snapshot for a single repository.

    Attributes:
        branch: Current checked-out branch.
        clean: True if the working tree has no uncommitted changes.
        ahead: Number of commits ahead of upstream.
        behind: Number of commits behind upstream.
    """

    branch: str
    clean: bool
    ahead: int
    behind: int


@dataclass
class Workspace:
    """A multi-repo workspace managed by Bernstein.

    Attributes:
        root: Absolute path to the workspace root directory.
        repos: List of repository configurations.
    """

    root: Path
    repos: list[RepoConfig] = field(default_factory=list[RepoConfig])

    @classmethod
    def from_config(cls, config: dict[str, Any], root: Path) -> Workspace:
        """Parse workspace config from the ``workspace:`` section of bernstein.yaml.

        Args:
            config: The parsed ``workspace`` dict from YAML.  Expected shape::

                {"repos": [{"name": "...", "path": "...", ...}, ...]}

            root: Workspace root directory (used to resolve relative paths).

        Returns:
            A populated Workspace instance.

        Raises:
            ValueError: If the config is malformed or missing required fields.
        """
        raw_repos: object = config.get("repos")
        if not isinstance(raw_repos, list):
            raise ValueError("workspace.repos must be a list")

        repo_configs: list[RepoConfig] = []
        seen_names: set[str] = set()
        entries: list[object] = cast("list[object]", raw_repos)
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                raise ValueError(f"Each repo entry must be a mapping, got {type(raw_entry).__name__}")
            entry: dict[str, object] = cast("dict[str, object]", raw_entry)
            name: object = entry.get("name")
            path_raw: object = entry.get("path")
            if not name or not isinstance(name, str):
                raise ValueError("Each repo must have a non-empty 'name' string")
            if not path_raw or not isinstance(path_raw, str):
                raise ValueError(f"Repo '{name}' must have a non-empty 'path' string")
            if name in seen_names:
                raise ValueError(f"Duplicate repo name: '{name}'")
            seen_names.add(name)

            url_raw: object = entry.get("url")
            url: str | None = str(url_raw) if url_raw is not None else None
            branch_raw: object = entry.get("branch", "main")
            branch: str = str(branch_raw)

            repo_configs.append(
                RepoConfig(
                    name=name,
                    path=Path(path_raw),
                    url=url,
                    branch=branch,
                )
            )

        return cls(root=root.resolve(), repos=repo_configs)

    def resolve_repo(self, name: str) -> Path:
        """Get the absolute path for a named repo.

        Args:
            name: Repository name as declared in the workspace config.

        Returns:
            Absolute path to the repo directory.

        Raises:
            KeyError: If no repo with that name exists.
        """
        for repo in self.repos:
            if repo.name == name:
                repo_path = repo.path
                if not repo_path.is_absolute():
                    repo_path = self.root / repo_path
                return repo_path.resolve()
        raise KeyError(f"Unknown repo: '{name}'")

    def clone_missing(self) -> list[str]:
        """Clone repos that don't exist locally via ``git clone``.

        Only repos with a configured ``url`` are considered.  Repos
        whose directories already exist are skipped.

        Returns:
            List of repo names that were successfully cloned.
        """
        cloned: list[str] = []
        for repo in self.repos:
            if repo.url is None:
                continue
            abs_path = repo.path if repo.path.is_absolute() else self.root / repo.path
            if abs_path.exists():
                logger.debug("Repo '%s' already exists at %s, skipping clone", repo.name, abs_path)
                continue

            abs_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", "--branch", repo.branch, repo.url, str(abs_path)]
            logger.info("Cloning %s -> %s", repo.url, abs_path)
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                cloned.append(repo.name)
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to clone '%s': %s", repo.name, exc.stderr.strip())

        return cloned

    def status(self) -> dict[str, RepoStatus]:
        """Get git status for each repo in the workspace.

        Returns:
            Mapping of repo name to its RepoStatus.  Repos whose
            directories don't exist or aren't git repos are skipped.
        """
        result: dict[str, RepoStatus] = {}
        for repo in self.repos:
            abs_path = repo.path if repo.path.is_absolute() else self.root / repo.path
            if not (abs_path / ".git").exists():
                continue

            branch = self._git_current_branch(abs_path)
            clean = self._git_is_clean(abs_path)
            ahead, behind = self._git_ahead_behind(abs_path)
            result[repo.name] = RepoStatus(
                branch=branch,
                clean=clean,
                ahead=ahead,
                behind=behind,
            )
        return result

    def validate(self) -> list[str]:
        """Check all repos exist and are valid git repos.

        Returns:
            List of human-readable issue descriptions.  Empty list
            means all repos are healthy.
        """
        issues: list[str] = []
        for repo in self.repos:
            abs_path = repo.path if repo.path.is_absolute() else self.root / repo.path
            if not abs_path.exists():
                issues.append(f"Repo '{repo.name}': path does not exist ({abs_path})")
            elif not (abs_path / ".git").exists():
                issues.append(f"Repo '{repo.name}': not a git repository ({abs_path})")
        return issues

    # -- private git helpers --------------------------------------------------

    @staticmethod
    def _git_current_branch(repo_path: Path) -> str:
        """Return the current branch name, or 'HEAD' if detached."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            return "unknown"

    @staticmethod
    def _git_is_clean(repo_path: Path) -> bool:
        """Return True if the working tree has no uncommitted changes."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip() == ""
        except (subprocess.CalledProcessError, OSError):
            return False

    @staticmethod
    def _git_ahead_behind(repo_path: Path) -> tuple[int, int]:
        """Return (ahead, behind) counts relative to upstream."""
        try:
            result = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except (subprocess.CalledProcessError, OSError, ValueError):
            pass
        return 0, 0
