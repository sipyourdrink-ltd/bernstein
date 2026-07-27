"""Warn when the audit chain's append lock cannot do its job (issue #3064).

Cross-process appends to ``.sdd/audit/<day>.jsonl`` serialise under
``flock(LOCK_EX)`` on a single ``.chain.lock`` per audit directory. That lock is
what keeps two processes from recovering the same chain tail and each appending
a record embedding the same stale ``prev_hmac``.

``flock`` is not universal. It is honoured on local filesystems and on NFSv4; it
is unreliable or client-local on NFSv3 without a working ``lockd``, on SMB/CIFS,
and on 9p and FUSE passthrough mounts; and on Windows the append section is a
documented no-op, because :mod:`fcntl` does not exist there and only in-process
locks remain.

An operator who puts ``.sdd`` on a network share therefore loses a guarantee
without any signal. This probe gives them the signal. It never fails the doctor
run: an unreliable lock is a deployment fact to weigh, not a broken install, and
turning ``bernstein doctor`` red for a laptop on a mounted share would train
operators to ignore it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.cli.doctor.report import DoctorResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

__all__ = [
    "CHECK_NAME",
    "UNRELIABLE_FILESYSTEMS",
    "check_audit_lock_filesystem",
    "fstype_from_bsd_mount",
    "fstype_from_mountinfo",
    "resolve_filesystem_type",
]

#: Stable identifier for the check, as it appears in the doctor table.
CHECK_NAME = "audit:chain-lock-filesystem"

#: Filesystem types where ``flock`` does not reliably serialise writers on
#: different hosts, mapped to what specifically goes wrong. Keys are matched
#: case-insensitively against the type the OS reports.
UNRELIABLE_FILESYSTEMS: dict[str, str] = {
    "nfs": "NFSv3 needs a working lockd for flock to reach the server; without it the lock is client-local",
    "nfs4": "NFSv4 carries locks in the protocol, but a server that drops lock state on restart still loses them",
    "smbfs": "SMB does not map flock onto a cross-client lock",
    "cifs": "CIFS does not map flock onto a cross-client lock",
    "smb2": "SMB does not map flock onto a cross-client lock",
    "9p": "9p passthrough mounts (VM and container shares) do not carry flock to the host",
    "virtiofs": "virtiofs shares do not carry flock across the guest boundary",
    "fuse.sshfs": "sshfs locks are client-local",
    "fuse.s3fs": "object-store FUSE mounts have no lock semantics at all",
    "overlay": "an overlay upper layer can hide writes from another mount namespace",
}

_REMEDIATION = (
    "Move the audit directory to a local filesystem, or give each host its own "
    "audit directory and reconcile the chains offline. See "
    "docs/cluster/deployment-patterns.md."
)


def fstype_from_mountinfo(target: Path, mountinfo_text: str) -> str | None:
    """Return the filesystem type serving ``target`` per Linux ``mountinfo``.

    Each line is ``id parent major:minor root mountpoint options [tags] - fstype
    source superopts``. The mount point is field 4 of the left half and the type
    is field 0 of the right half; the optional tags before the ``-`` are what
    makes positional parsing of the whole line wrong.

    The longest matching mount point wins, so a bind mount nested inside another
    filesystem is attributed to the nested mount rather than to its parent.

    Args:
        target: An absolute, resolved path.
        mountinfo_text: Contents of ``/proc/self/mountinfo``.

    Returns:
        The filesystem type, or ``None`` when no mount point covers ``target``.
    """
    best: tuple[int, str] | None = None
    for line in mountinfo_text.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        mount_point = left_fields[4].replace("\\040", " ")
        fstype = right_fields[0]
        if not _covers(mount_point, target):
            continue
        if best is None or len(mount_point) > best[0]:
            best = (len(mount_point), fstype)
    return None if best is None else best[1]


def fstype_from_bsd_mount(target: Path, mount_text: str) -> str | None:
    """Return the filesystem type serving ``target`` per BSD ``mount`` output.

    Lines read ``<source> on <mount point> (<type>, <flags...>)``. macOS has no
    ``/proc``, and :func:`os.statvfs` does not expose ``f_fstypename`` through
    Python, so the mount table is the portable source of the answer.

    Args:
        target: An absolute, resolved path.
        mount_text: Output of ``mount`` with no arguments.

    Returns:
        The filesystem type, or ``None`` when no mount point covers ``target``.
    """
    best: tuple[int, str] | None = None
    for line in mount_text.splitlines():
        head, separator, tail = line.partition(" on ")
        if not separator or not head:
            continue
        mount_point, paren, flags = tail.partition(" (")
        if not paren:
            continue
        fstype = flags.split(",")[0].rstrip(")").strip()
        if not fstype or not _covers(mount_point, target):
            continue
        if best is None or len(mount_point) > best[0]:
            best = (len(mount_point), fstype)
    return None if best is None else best[1]


def _covers(mount_point: str, target: Path) -> bool:
    """Whether ``mount_point`` is ``target`` or one of its ancestors."""
    try:
        mount = Path(mount_point)
    except ValueError:  # pragma: no cover - defensive
        return False
    return target == mount or mount in target.parents


def resolve_filesystem_type(
    path: Path,
    *,
    mountinfo: Path | None = None,
    mount_command: tuple[str, ...] = ("mount",),
    platform: str | None = None,
) -> str | None:
    """Return the filesystem type serving ``path``, or ``None`` if unknown.

    Best-effort by construction: an unknown type downgrades the check to a skip
    rather than guessing, because a wrong warning about the audit directory is
    worse than no warning.

    Args:
        path: Directory to resolve. Need not exist; the nearest existing
            ancestor is used, which is what a not-yet-created ``.sdd/audit``
            needs.
        mountinfo: Override for ``/proc/self/mountinfo`` (tests).
        mount_command: Override for the BSD ``mount`` invocation (tests).
        platform: Override for :data:`sys.platform` (tests).
    """
    plat = sys.platform if platform is None else platform
    target = _nearest_existing(path)

    source = Path("/proc/self/mountinfo") if mountinfo is None else mountinfo
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if text:
        return fstype_from_mountinfo(target, text)

    if plat.startswith(("darwin", "freebsd", "openbsd", "netbsd")):
        try:
            completed = subprocess.run(
                list(mount_command),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return fstype_from_bsd_mount(target, completed.stdout)

    return None


def _nearest_existing(path: Path) -> Path:
    """Return ``path`` resolved, or its nearest existing resolved ancestor."""
    resolved = Path(path).expanduser().resolve()
    candidate = resolved
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def check_audit_lock_filesystem(
    audit_dir: Path,
    *,
    has_fcntl: bool | None = None,
    filesystem: str | None = None,
    advisories: Mapping[str, str] | None = None,
) -> DoctorResult:
    """Report whether the audit chain's append lock is reliable where it lives.

    Args:
        audit_dir: The audit directory, typically ``.sdd/audit``.
        has_fcntl: Override for whether :mod:`fcntl` is importable (tests).
        filesystem: Override for the resolved filesystem type (tests).
        advisories: Override for :data:`UNRELIABLE_FILESYSTEMS` (tests).

    Returns:
        A :class:`DoctorResult`. Never ``fail``: this reports a property of the
        deployment, and an operator on a mounted share has a working install.
    """
    table = UNRELIABLE_FILESYSTEMS if advisories is None else advisories
    if has_fcntl is None:
        from bernstein.core.security.audit import fcntl

        has_fcntl = fcntl is not None

    if not has_fcntl:
        return DoctorResult(
            name=CHECK_NAME,
            category="environment",
            status="warn",
            detail=(
                "this platform has no fcntl, so the audit chain's cross-process "
                "append lock is a no-op; only threads inside one process are ordered"
            ),
            remediation="Run the writers that share one audit directory in a single process.",
        )

    fstype = resolve_filesystem_type(audit_dir) if filesystem is None else filesystem
    if not fstype:
        return DoctorResult(
            name=CHECK_NAME,
            category="environment",
            status="skip",
            detail=f"could not determine the filesystem serving {audit_dir}",
        )

    advisory = table.get(fstype.lower())
    if advisory is None:
        return DoctorResult(
            name=CHECK_NAME,
            category="environment",
            status="ok",
            detail=f"{audit_dir} is on {fstype}; flock serialises cross-process appends here",
        )
    return DoctorResult(
        name=CHECK_NAME,
        category="environment",
        status="warn",
        detail=f"{audit_dir} is on {fstype}: {advisory}",
        remediation=_REMEDIATION,
    )
