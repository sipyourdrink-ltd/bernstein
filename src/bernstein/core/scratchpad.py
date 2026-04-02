"""Scratchpad directory management for cross-worker state."""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class ScratchpadManager:
    """Manage scratchpad directory for cross-worker state sharing.

    Provides a shared directory where worker agents can read/write
    durable notes and artifacts without triggering permission prompts.
    Stays outside the main git worktree when appropriate.

    Args:
        workdir: Project working directory.
        run_id: Unique run identifier.
        auto_cleanup: Whether to auto-cleanup on destruction.
    """

    def __init__(
        self,
        workdir: Path,
        run_id: str | None = None,
        auto_cleanup: bool = True,
    ) -> None:
        self._workdir = workdir
        self._run_id = run_id or "default"
        self._auto_cleanup = auto_cleanup
        self._scratchpad_path: Path | None = None

    @property
    def scratchpad_path(self) -> Path | None:
        """Get the scratchpad directory path."""
        return self._scratchpad_path

    def create_scratchpad(self) -> Path:
        """Create the scratchpad directory.

        Returns:
            Path to the created scratchpad directory.
        """
        # Create in .sdd/runtime/scratchpad/{run_id}
        self._scratchpad_path = self._workdir / ".sdd" / "runtime" / "scratchpad" / self._run_id
        self._scratchpad_path.mkdir(parents=True, exist_ok=True)

        # Set safe permissions (owner read/write/execute only)
        self._scratchpad_path.chmod(0o700)

        logger.info(
            "Created scratchpad directory at %s",
            self._scratchpad_path,
        )

        return self._scratchpad_path

    def get_worker_scratchpad(self, worker_id: str) -> Path:
        """Get or create a worker-specific scratchpad subdirectory.

        Args:
            worker_id: Unique worker identifier.

        Returns:
            Path to the worker's scratchpad subdirectory.
        """
        if self._scratchpad_path is None:
            self.create_scratchpad()

        assert self._scratchpad_path is not None

        worker_scratchpad = self._scratchpad_path / worker_id
        worker_scratchpad.mkdir(parents=True, exist_ok=True)
        worker_scratchpad.chmod(0o700)

        return worker_scratchpad

    def get_shared_file(self, filename: str) -> Path:
        """Get path to a shared file in the scratchpad.

        Args:
            filename: Name of the shared file.

        Returns:
            Path to the shared file.
        """
        if self._scratchpad_path is None:
            self.create_scratchpad()

        assert self._scratchpad_path is not None

        return self._scratchpad_path / filename

    def write_shared_note(self, filename: str, content: str) -> Path:
        """Write a shared note to the scratchpad.

        Args:
            filename: Name of the note file.
            content: Note content.

        Returns:
            Path to the written file.
        """
        file_path = self.get_shared_file(filename)
        file_path.write_text(content, encoding="utf-8")
        file_path.chmod(0o600)

        logger.debug("Wrote shared note to %s", file_path)
        return file_path

    def read_shared_note(self, filename: str) -> str | None:
        """Read a shared note from the scratchpad.

        Args:
            filename: Name of the note file.

        Returns:
            Note content or None if not found.
        """
        file_path = self.get_shared_file(filename)
        if not file_path.exists():
            return None

        return file_path.read_text(encoding="utf-8")

    def list_shared_files(self) -> list[Path]:
        """List all shared files in the scratchpad.

        Returns:
            List of file paths.
        """
        if self._scratchpad_path is None:
            return []

        return [f for f in self._scratchpad_path.iterdir() if f.is_file()]

    def cleanup(self) -> int:
        """Clean up the scratchpad directory.

        Returns:
            Number of files/directories removed.
        """
        if self._scratchpad_path is None or not self._scratchpad_path.exists():
            return 0

        count = 0
        try:
            for item in self._scratchpad_path.iterdir():
                if item.is_file():
                    item.unlink()
                    count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    count += 1

            self._scratchpad_path.rmdir()
            self._scratchpad_path = None

            logger.info("Cleaned up scratchpad directory (%d items)", count)
        except Exception as exc:
            logger.warning("Failed to cleanup scratchpad: %s", exc)

        return count

    def __enter__(self) -> ScratchpadManager:
        """Context manager entry."""
        self.create_scratchpad()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        if self._auto_cleanup:
            self.cleanup()

    def get_env_vars(self) -> dict[str, str]:
        """Get environment variables to pass to spawned agents.

        Returns:
            Dictionary of environment variables.
        """
        if self._scratchpad_path is None:
            return {}

        return {
            "BERNSTEIN_SCRATCHPAD": str(self._scratchpad_path),
            "BERNSTEIN_SCRATCHPAD_SHARED": str(self._scratchpad_path / "shared"),
        }

    def get_prompt_contract(self) -> str:
        """Get prompt contract text for agents about scratchpad usage.

        Returns:
            Prompt contract string.
        """
        if self._scratchpad_path is None:
            return ""

        return f"""
## Scratchpad Directory

You have access to a shared scratchpad directory for cross-worker state:
- Path: `{self._scratchpad_path}`
- Use this for: notes, artifacts, intermediate results
- Do NOT commit scratchpad files to git
- Files here are temporary and may be cleaned up after the run

Shared files (visible to all workers):
- `{self._scratchpad_path}/shared/`

Worker-specific files:
- `{self._scratchpad_path}/<worker_id>/`

Write notes for other workers to read when coordination is needed.
"""
