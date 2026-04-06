"""Git hook installation for file permission enforcement in worktrees.

Installs a pre-commit hook that checks staged files against the agent's
denied paths.  If any staged file matches a denied pattern, the commit
is blocked.

Usage::

    installer = GitHookInstaller(denied_paths=(".sdd/*", ".github/*"))
    installer.install(worktree_path)
"""

from __future__ import annotations

import logging
import os
import stat
import textwrap
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_HOOK_MARKER: Final[str] = "# BERNSTEIN-MANAGED-HOOK"

_PRE_COMMIT_TEMPLATE: Final[str] = textwrap.dedent("""\
    #!/usr/bin/env python3
    {marker}
    # Auto-installed by Bernstein to enforce file permission boundaries.
    # Denied path patterns: {denied_paths_repr}
    # Do not edit manually — this hook is regenerated on agent spawn.

    import fnmatch
    import subprocess
    import sys

    DENIED_PATTERNS = {denied_paths_list}

    def path_matches_any(filepath: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            if fnmatch.fnmatch(filepath, pattern):
                return True
            if pattern.endswith("/*"):
                prefix = pattern[:-1]
                if filepath.startswith(prefix):
                    return True
        return False

    def main() -> int:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            print("bernstein pre-commit: failed to get staged files", file=sys.stderr)
            return 1

        staged = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        violations = [f for f in staged if path_matches_any(f, DENIED_PATTERNS)]

        if violations:
            print("bernstein pre-commit: BLOCKED — files outside permitted scope:", file=sys.stderr)
            for v in violations:
                print(f"  - {{v}}", file=sys.stderr)
            return 1

        return 0

    if __name__ == "__main__":
        sys.exit(main())
""")


class GitHookInstaller:
    """Install pre-commit hooks that enforce file permission boundaries.

    Args:
        denied_paths: Glob patterns for paths the agent must not commit to.
    """

    def __init__(self, denied_paths: tuple[str, ...] | list[str] = ()) -> None:
        self._denied_paths = list(denied_paths)

    @property
    def denied_paths(self) -> list[str]:
        """The denied path patterns this hook enforces."""
        return list(self._denied_paths)

    def install(self, worktree_path: str | Path) -> Path:
        """Install the pre-commit hook in a worktree.

        Creates or overwrites the ``.git/hooks/pre-commit`` file.  If the
        worktree uses a ``.git`` file (gitdir redirect), the hook is
        installed in the actual git directory.

        Args:
            worktree_path: Path to the worktree root.

        Returns:
            Path to the installed hook script.

        Raises:
            FileNotFoundError: If the worktree path does not exist.
        """
        wt = Path(worktree_path)
        if not wt.exists():
            msg = f"Worktree path does not exist: {wt}"
            raise FileNotFoundError(msg)

        hooks_dir = self._find_hooks_dir(wt)
        hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_path = hooks_dir / "pre-commit"

        # Generate the hook script
        content = _PRE_COMMIT_TEMPLATE.format(
            marker=_HOOK_MARKER,
            denied_paths_repr=repr(self._denied_paths),
            denied_paths_list=repr(self._denied_paths),
        )

        hook_path.write_text(content, encoding="utf-8")

        # Make executable
        current_mode = hook_path.stat().st_mode
        hook_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        logger.info(
            "Installed pre-commit hook at %s (denied: %s)",
            hook_path,
            self._denied_paths,
        )
        return hook_path

    def uninstall(self, worktree_path: str | Path) -> bool:
        """Remove the Bernstein pre-commit hook from a worktree.

        Only removes the hook if it was installed by Bernstein (contains
        the marker comment).

        Args:
            worktree_path: Path to the worktree root.

        Returns:
            True if a hook was removed, False if no Bernstein hook was found.
        """
        wt = Path(worktree_path)
        hooks_dir = self._find_hooks_dir(wt)
        hook_path = hooks_dir / "pre-commit"

        if not hook_path.exists():
            return False

        content = hook_path.read_text(encoding="utf-8")
        if _HOOK_MARKER not in content:
            logger.debug("Pre-commit hook at %s is not Bernstein-managed", hook_path)
            return False

        hook_path.unlink()
        logger.info("Removed Bernstein pre-commit hook from %s", hook_path)
        return True

    def is_installed(self, worktree_path: str | Path) -> bool:
        """Check whether a Bernstein pre-commit hook is installed.

        Args:
            worktree_path: Path to the worktree root.

        Returns:
            True if a Bernstein-managed hook exists.
        """
        wt = Path(worktree_path)
        hooks_dir = self._find_hooks_dir(wt)
        hook_path = hooks_dir / "pre-commit"

        if not hook_path.exists():
            return False

        content = hook_path.read_text(encoding="utf-8")
        return _HOOK_MARKER in content

    @staticmethod
    def _find_hooks_dir(worktree_path: Path) -> Path:
        """Locate the git hooks directory for a worktree.

        Handles both regular repos (``.git/hooks``) and worktrees where
        ``.git`` is a file containing a ``gitdir:`` redirect.

        Args:
            worktree_path: Path to the worktree root.

        Returns:
            Path to the hooks directory.
        """
        git_path = worktree_path / ".git"

        if git_path.is_file():
            # .git file with gitdir redirect
            text = git_path.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = text.split(":", 1)[1].strip()
                if not os.path.isabs(gitdir):
                    gitdir = str((worktree_path / gitdir).resolve())
                return Path(gitdir) / "hooks"

        return git_path / "hooks"
