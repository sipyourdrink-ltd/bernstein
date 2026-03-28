"""Multi-repo workspace coordinator for Bernstein.

Allows Bernstein to orchestrate tasks across multiple repositories.
Workspace configuration lives in the seed file under the ``workspace:`` key.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RepoConfig:
    """Configuration for a single repository in a workspace.

    Attributes:
        name: Short identifier used by tasks to reference this repo.
        path: Local filesystem path (may be relative to workspace root).
        url: Optional remote URL for cloning (git@github.com:org/repo.git).
        branch: Default branch to check out (default: "main").
    """

    name: str
    path: Path
    url: str | None = None
    branch: str = "main"


@dataclass
class Workspace:
    """A multi-repository workspace managed by Bernstein.

    Attributes:
        root: The root directory of the workspace (usually the project root).
        repos: Ordered list of repository configurations.
    """

    root: Path
    repos: list[RepoConfig] = field(default_factory=list[RepoConfig])

    # ---------------------------------------------------------------------------
    # Construction
    # ---------------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any], root: Path) -> Workspace:
        """Build a Workspace from a parsed YAML config dict.

        The ``config`` dict corresponds to the ``workspace:`` section of
        ``bernstein.yaml``.  Each entry under ``repos`` must have at least a
        ``name`` and a ``path``.

        Args:
            config: Parsed workspace config mapping (``repos`` list expected).
            root: Workspace root directory used to resolve relative paths.

        Returns:
            Populated Workspace instance.

        Raises:
            ValueError: If a repo entry is missing ``name`` or ``path``.
        """
        repos_raw: object = config.get("repos", [])
        if not isinstance(repos_raw, list):
            raise ValueError(f"workspace.repos must be a list, got {type(repos_raw).__name__}")

        repos: list[RepoConfig] = []
        for entry in repos_raw:
            if not isinstance(entry, dict):
                raise ValueError(f"Each workspace repo entry must be a mapping, got {type(entry).__name__}")
            name: object = entry.get("name")
            if not name or not isinstance(name, str):
                raise ValueError(f"Each workspace repo entry must have a non-empty 'name', got: {entry!r}")
            raw_path: object = entry.get("path")
            if not raw_path or not isinstance(raw_path, str):
                raise ValueError(f"Each workspace repo entry must have a non-empty 'path', got: {entry!r}")

            resolved = Path(raw_path)
            if not resolved.is_absolute():
                resolved = (root / resolved).resolve()

            url_raw: object = entry.get("url")
            url: str | None = str(url_raw) if url_raw is not None else None
            branch_raw: object = entry.get("branch", "main")
            branch: str = str(branch_raw)

            repos.append(RepoConfig(name=str(name), path=resolved, url=url, branch=branch))

        return cls(root=root, repos=repos)

    # ---------------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------------

    def resolve_repo(self, repo_name: str) -> Path:
        """Return the local path for a named repository.

        Args:
            repo_name: The ``name`` field of the desired repo.

        Returns:
            Absolute path to the local repository directory.

        Raises:
            KeyError: If no repo with the given name is configured.
        """
        for repo in self.repos:
            if repo.name == repo_name:
                return repo.path
        raise KeyError(f"No repo named {repo_name!r} in workspace. Known repos: {[r.name for r in self.repos]}")

    # ---------------------------------------------------------------------------
    # Operations
    # ---------------------------------------------------------------------------

    def clone_missing(self) -> list[str]:
        """Clone any repos whose local paths do not yet exist.

        Uses ``git clone`` with the configured URL and branch.  Repos without
        a URL configured are skipped with a warning.

        Returns:
            List of repo names that were successfully cloned.

        Raises:
            RuntimeError: If ``git clone`` fails for any repo.
        """
        cloned: list[str] = []
        for repo in self.repos:
            if repo.path.exists():
                logger.debug("Repo '%s' already exists at %s, skipping", repo.name, repo.path)
                continue
            if repo.url is None:
                logger.warning(
                    "Repo '%s' at %s does not exist and has no URL — cannot clone",
                    repo.name,
                    repo.path,
                )
                continue
            logger.info("Cloning %s from %s into %s", repo.name, repo.url, repo.path)
            cmd = ["git", "clone", "--branch", repo.branch, repo.url, str(repo.path)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed for repo '{repo.name}': {result.stderr.strip()}")
            cloned.append(repo.name)
            logger.info("Cloned '%s' successfully", repo.name)
        return cloned

    def status(self) -> dict[str, dict[str, str]]:
        """Return git status information for each configured repository.

        For each repo, runs ``git rev-parse``, ``git status``, and
        ``git rev-list`` to collect branch, clean/dirty state, and
        ahead/behind counts.

        Returns:
            Mapping of repo name → status dict with keys:
            - ``branch``: current branch name (or ``"(detached)"``).
            - ``state``: ``"clean"`` or ``"dirty"``.
            - ``ahead``: number of commits ahead of upstream.
            - ``behind``: number of commits behind upstream.
            - ``error``: error message if the repo could not be queried.
        """
        result: dict[str, dict[str, str]] = {}
        for repo in self.repos:
            result[repo.name] = _repo_status(repo)
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr).

    Args:
        args: Git subcommand and arguments (without the leading "git").
        cwd: Working directory.

    Returns:
        Tuple of (returncode, stdout, stderr).
    """
    completed = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _repo_status(repo: RepoConfig) -> dict[str, str]:
    """Collect git status for a single repo.

    Args:
        repo: Repository configuration.

    Returns:
        Status dict with branch, state, ahead, behind, and optional error.
    """
    if not repo.path.exists():
        return {"error": f"path does not exist: {repo.path}"}

    # Branch name
    rc, branch_out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo.path)
    if rc != 0:
        return {"error": f"not a git repo or git error at {repo.path}"}
    branch = branch_out if branch_out else "(detached)"

    # Clean/dirty
    rc_s, status_out, _ = _run_git(["status", "--porcelain"], repo.path)
    state = "dirty" if (rc_s == 0 and status_out) else "clean"

    # Ahead / behind relative to @{u} (upstream tracking branch)
    ahead = "0"
    behind = "0"
    rc_ab, ab_out, _ = _run_git(
        ["rev-list", "--left-right", "--count", f"{branch}...@{{u}}"],
        repo.path,
    )
    if rc_ab == 0 and "\t" in ab_out:
        parts = ab_out.split("\t", 1)
        ahead = parts[0].strip()
        behind = parts[1].strip()

    return {"branch": branch, "state": state, "ahead": ahead, "behind": behind}
