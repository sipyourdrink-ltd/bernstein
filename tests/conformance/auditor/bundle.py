"""Bundle-only reader for the auditor conformance suite.

An auditor holds the exported bundle and nothing else. A vector that
quietly reads ``.sdd/`` - or the repository, or the operator's key
material - answers a question the auditor could not have answered, and
the suite stops measuring what it claims to measure.

:class:`BundleReader` is the only file access the vectors get. Every
lookup is containment-checked *after* resolution, so a ``..`` segment, an
absolute path and a symlink pointing out of the export are all refused
the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class BundleBoundaryError(RuntimeError):
    """A lookup tried to leave the exported bundle."""


@dataclass(frozen=True, slots=True)
class BundleReader:
    """Read-only access confined to one exported bundle.

    Attributes:
        root: Resolved directory the bundle was exported into.
    """

    root: Path

    @classmethod
    def open(cls, root: Path) -> BundleReader:
        """Open the bundle rooted at *root*.

        Args:
            root: Directory holding the exported bundle.

        Returns:
            A reader confined to that directory.

        Raises:
            BundleBoundaryError: *root* is not a directory.
        """
        resolved = root.resolve()
        if not resolved.is_dir():
            raise BundleBoundaryError(f"not a bundle directory: {root}")
        return cls(root=resolved)

    def resolve(self, name: str) -> Path:
        """Return the in-bundle path *name* points at.

        Args:
            name: Bundle-relative POSIX path, e.g. ``"run-receipt.json"``.

        Returns:
            The resolved path, proven to sit inside :attr:`root`.

        Raises:
            BundleBoundaryError: *name* is absolute, walks up out of the
                bundle, resolves outside it (a symlink escape), or names
                nothing.
        """
        pure = PurePosixPath(name)
        if pure.is_absolute() or Path(name).is_absolute():
            raise BundleBoundaryError(f"absolute paths are outside the bundle: {name}")
        if any(part == ".." for part in pure.parts):
            raise BundleBoundaryError(f"parent traversal is outside the bundle: {name}")

        candidate = (self.root / pure).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise BundleBoundaryError(f"resolves outside the bundle: {name} -> {candidate}")
        if not candidate.exists():
            raise BundleBoundaryError(f"not in the bundle: {name}")
        return candidate

    def read_bytes(self, name: str) -> bytes:
        """Return the bytes of the in-bundle file *name*.

        Args:
            name: Bundle-relative POSIX path.

        Returns:
            The file's bytes.

        Raises:
            BundleBoundaryError: *name* leaves the bundle or is not a file.
        """
        path = self.resolve(name)
        if not path.is_file():
            raise BundleBoundaryError(f"not a file in the bundle: {name}")
        return path.read_bytes()

    def read_json(self, name: str) -> Any:
        """Return the parsed JSON of the in-bundle file *name*.

        Args:
            name: Bundle-relative POSIX path.

        Returns:
            The decoded JSON document.

        Raises:
            BundleBoundaryError: *name* leaves the bundle or is not a file.
        """
        return json.loads(self.read_bytes(name).decode("utf-8"))

    def names(self) -> tuple[str, ...]:
        """Return every file in the bundle as a sorted bundle-relative path."""
        return tuple(sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*") if path.is_file()))


__all__ = ["BundleBoundaryError", "BundleReader"]
