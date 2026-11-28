"""Source abstraction for reading live ``.sdd/`` workspaces or verified archives."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager
    from pathlib import Path


@runtime_checkable
class RunSource(Protocol):
    """Readable root of ``.sdd/`` run evidence, live or archived."""

    @property
    def kind(self) -> Literal["live", "archive"]:
        """Origin kind: ``'live'`` for a workspace, ``'archive'`` for a bundle."""
        ...

    @property
    def location(self) -> Path:
        """Path to the workspace directory or archive bundle."""
        ...

    def open_workdir(self) -> AbstractContextManager[Path]:
        """Yield a readable workspace root containing ``.sdd/``."""
        ...


@dataclass(frozen=True)
class LiveRunSource:
    """Evidence source backed by a live workspace directory."""

    workdir: Path

    @property
    def kind(self) -> Literal["live", "archive"]:
        """Origin kind for a live workspace."""
        return "live"

    @property
    def location(self) -> Path:
        """Resolved workspace root path."""
        return self.workdir

    @contextmanager
    def open_workdir(self) -> Generator[Path]:
        """Yield the resolved workspace directory."""
        yield self.workdir.resolve()


@dataclass(frozen=True)
class ArchiveRunSource:
    """Evidence source backed by a verified ZIP run archive."""

    archive_path: Path
    opener: Callable[[Path], AbstractContextManager[Path]]

    @property
    def kind(self) -> Literal["live", "archive"]:
        """Origin kind for an archive bundle."""
        return "archive"

    @property
    def location(self) -> Path:
        """Resolved archive bundle path."""
        return self.archive_path

    def open_workdir(self) -> AbstractContextManager[Path]:
        """Verify and yield a readable extracted root for the archive."""
        return self.opener(self.archive_path)
