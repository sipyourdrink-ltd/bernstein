"""Open a file below a trusted root without following a symlink on the way.

``O_NOFOLLOW`` refuses a symlink at the **final** path component only.  A
store that shards its files into subdirectories therefore keeps an opening: a
symlink planted at a parent component -- ``.sdd/cas/ab/`` for a CAS blob --
is still followed, and the read lands wherever it points.

:func:`open_anchored` closes that by walking the path one component at a
time.  Each component is opened relative to a descriptor for its parent, with
``O_NOFOLLOW`` among the flags, so the refusal is a property of the open
itself.  Nothing re-derives a path between the check and the use, because
there is no check -- the same reasoning that put ``O_NOFOLLOW`` on the blob
open in the first place, extended to the components above it.

The **root is opened normally**, deliberately.  Pointing a store's root
somewhere else -- ``.sdd/cas`` symlinked onto a larger volume -- is ordinary
operator configuration, and refusing it would break working installations to
defend against an attacker who, by definition, already controls the location
the operator chose.  Everything below the root is store-managed layout, where
a symlink is never something the store itself wrote.

The walk needs ``os.open`` to accept ``dir_fd`` **and** the platform to define
``O_NOFOLLOW``.  Windows has neither, so there it degrades to a single open of
the joined path -- today's behaviour, no worse -- rather than to a
``is_symlink()`` pre-check, which would trade an atomic guarantee for a race
window and call it an improvement.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ANCHORED_OPEN_SUPPORTED", "open_anchored"]


# Both capabilities are required, not just ``dir_fd``: the anchored open
# refuses a symlink only because ``O_NOFOLLOW`` is among its flags, so
# selecting the walk on ``dir_fd`` alone would, on a platform offering one
# without the other, take the branch that cannot refuse anything.  This mirrors
# the reasoning in ``core.security.sigstore_attestation``.
ANCHORED_OPEN_SUPPORTED: bool = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


def open_anchored(root: Path, *components: str, flags: int) -> int:
    """Open ``root`` joined with *components*, refusing symlinked components.

    Args:
        root: Trusted anchor directory.  Opened without ``O_NOFOLLOW``: a
            symlinked root is operator configuration, not an attack.
        *components: Path components below *root*, outermost first.  Each is a
            single name; passing a separator, ``.`` or ``..`` is a programming
            error and is refused rather than resolved.
        flags: Open flags for the final component.  ``O_NOFOLLOW`` is added
            where the platform defines it; the caller supplies the rest
            (typically ``os.O_RDONLY``).

    Returns:
        An open file descriptor for the final component.  The caller owns it
        and must close it.

    Raises:
        ValueError: If no components are given, or one is not a single
            component name.
        OSError: As :func:`os.open` does.  Two errno values carry meaning for
            callers: ``ENOENT`` (``FileNotFoundError``) means a component is
            genuinely missing, and ``ELOOP`` means one was a symlink and the
            open refused to follow it.  Callers that treat a missing file as
            an ordinary miss must not widen that to ``OSError``, or a refused
            symlink would be reported as absence.
    """
    if not components:
        msg = "open_anchored requires at least one path component below the root"
        raise ValueError(msg)
    separators = tuple(sep for sep in (os.sep, os.altsep) if sep)
    for component in components:
        if component in {"", ".", ".."} or any(sep in component for sep in separators):
            msg = f"open_anchored components must be single names, got {component!r}"
            raise ValueError(msg)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not ANCHORED_OPEN_SUPPORTED:
        # Windows and anything else missing either capability.  One open of the
        # joined path: the final component keeps whatever protection the
        # platform offers, and the parents keep none, which is exactly where
        # this function came in.
        return os.open(root.joinpath(*components), flags | nofollow)

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(root, os.O_RDONLY | directory_flag)
    try:
        for component in components[:-1]:
            # ``O_DIRECTORY`` makes a non-directory component fail as ENOTDIR
            # here rather than at the next open, so the errno names what is
            # actually wrong.
            child_fd = os.open(
                component,
                os.O_RDONLY | nofollow | directory_flag,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child_fd
        return os.open(components[-1], flags | nofollow, dir_fd=parent_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_fd)
