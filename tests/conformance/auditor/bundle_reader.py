"""The auditor's whole world: a reader that can only open the bundle.

Every vector in this suite answers its question from an exported bundle
and nothing else. That constraint is not a review convention here - it
is enforced. :class:`BundleReader` composes every path through the
production containment check and refuses anything that resolves outside
the bundle root, so a vector cannot reach ``.sdd/``, the source tree, or
the workspace that produced the evidence even by accident.

The failure mode this exists to prevent is a green suite that is green
only because the test process happens to sit on the machine that made
the run. An auditor does not have that machine.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from bernstein.core.security.path_containment import PathContainmentError, contained_path


class BundleBoundaryError(RuntimeError):
    """A read was attempted outside the exported bundle."""


class BundleReader:
    """Read-only view over one exported evidence bundle.

    Args:
        root: The bundle directory. Must exist.

    Raises:
        BundleBoundaryError: *root* is not an existing directory.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise BundleBoundaryError(f"not a bundle directory: {root}")
        self._root = resolved

    @property
    def root(self) -> Path:
        """The bundle directory. The only directory this reader may open."""
        return self._root

    def names(self) -> list[str]:
        """Return the bundle's file names, sorted."""
        return sorted(entry.name for entry in self._root.iterdir() if entry.is_file())

    def path(self, name: str) -> Path:
        """Return the containment-checked path of *name* inside the bundle.

        Args:
            name: A single file name inside the bundle.

        Returns:
            The resolved path, proven to sit under :attr:`root`.

        Raises:
            BundleBoundaryError: *name* is not a plain name inside the
                bundle, or resolves outside it (``..``, an absolute
                path, or a symlink pointing out).
        """
        try:
            return contained_path(self._root, name, label="bundle entry")
        except PathContainmentError as exc:
            raise BundleBoundaryError(
                f"refusing to read {name!r}: the auditor only has the bundle ({exc})",
            ) from exc

    def read_bytes(self, name: str) -> bytes:
        """Return the raw bytes of the bundle entry *name*."""
        return self.path(name).read_bytes()

    def read_json(self, name: str) -> Any:
        """Return the parsed JSON of the bundle entry *name*."""
        return json.loads(self.read_bytes(name).decode("utf-8"))

    def read_zip_member(self, name: str, member: str) -> bytes:
        """Return one member of a zip held in the bundle.

        Args:
            name: The zip entry inside the bundle.
            member: The member to read out of it.

        Returns:
            The member's bytes.

        Raises:
            BundleBoundaryError: *name* is outside the bundle, or
                *member* is an absolute or traversing path inside the
                archive.
        """
        if member.startswith("/") or ".." in Path(member).parts:
            raise BundleBoundaryError(f"refusing to read archive member {member!r}: it escapes the archive")
        with zipfile.ZipFile(self.path(name)) as archive:
            return archive.read(member)


__all__ = ["BundleBoundaryError", "BundleReader"]
