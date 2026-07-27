"""The doctor probe for the audit chain's append lock (issue #3064).

The chain's cross-process guarantee is an advisory ``flock`` on one lock file
per audit directory. That is a guarantee about a *filesystem*, not about the
product: an operator who puts ``.sdd`` on an SMB share or in a 9p guest mount
loses it with no signal at all. This probe is the signal.

Both mount-table parsers are exercised against captured text rather than
against the machine running the test, so the Linux path is covered on macOS and
the BSD path is covered on Linux. Anything the parser cannot answer degrades to
a skip: a confident wrong answer about where the audit chain lives is worse than
no answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.cli.doctor.audit_lock_checks import (
    CHECK_NAME,
    UNRELIABLE_FILESYSTEMS,
    check_audit_lock_filesystem,
    fstype_from_bsd_mount,
    fstype_from_mountinfo,
    resolve_filesystem_type,
)

# A trimmed /proc/self/mountinfo. The nfs line carries an optional field
# (``shared:1``) before the ``-`` separator, which is exactly what breaks a
# parser that splits the whole line positionally.
_MOUNTINFO = """\
23 28 0:21 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
25 28 0:6 / /dev rw,nosuid - devtmpfs udev rw,size=4045748k
28 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw
41 28 0:38 / /srv/shared rw,relatime shared:1 - nfs4 10.0.0.4:/export rw,vers=4.2
44 28 0:39 / /mnt/win rw,relatime - cifs //fileserver/share rw
47 28 0:40 / /mnt/host rw,relatime - 9p host9p rw,trans=virtio
"""

_BSD_MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
//guest@nas._smb._tcp.local/media on /Volumes/media (smbfs, nodev, nosuid, mounted by op)
"""


# ---------------------------------------------------------------------------
# Linux mountinfo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "ext4"),
        ("/home/op/proj/.sdd/audit", "ext4"),
        ("/srv/shared", "nfs4"),
        ("/srv/shared/team/.sdd/audit", "nfs4"),
        ("/mnt/win/.sdd/audit", "cifs"),
        ("/mnt/host/work/.sdd/audit", "9p"),
        ("/proc/self", "proc"),
    ],
)
def test_mountinfo_attributes_a_path_to_its_longest_mount(path: str, expected: str) -> None:
    """The nested mount wins, not its parent."""
    assert fstype_from_mountinfo(Path(path), _MOUNTINFO) == expected


def test_mountinfo_ignores_lines_without_the_separator() -> None:
    """A truncated table yields no answer rather than a wrong one."""
    assert fstype_from_mountinfo(Path("/srv/shared"), "23 28 0:21 / /srv/shared rw\n") is None


def test_mountinfo_decodes_an_escaped_space_in_a_mount_point() -> None:
    table = "28 1 8:1 / /mnt/my\\040share rw,relatime - cifs //srv/s rw\n"
    assert fstype_from_mountinfo(Path("/mnt/my share/.sdd/audit"), table) == "cifs"


# ---------------------------------------------------------------------------
# BSD mount
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/op/proj/.sdd/audit", "apfs"),
        ("/System/Volumes/Data/anything", "apfs"),
        ("/Volumes/media/team/.sdd/audit", "smbfs"),
        ("/dev/null", "devfs"),
    ],
)
def test_bsd_mount_attributes_a_path_to_its_longest_mount(path: str, expected: str) -> None:
    assert fstype_from_bsd_mount(Path(path), _BSD_MOUNT) == expected


def test_bsd_mount_ignores_lines_it_cannot_parse() -> None:
    assert fstype_from_bsd_mount(Path("/Volumes/media"), "some banner line\n") is None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolution_uses_mountinfo_when_it_exists(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"28 1 8:1 / {tmp_path} rw,relatime - nfs srv:/e rw\n", encoding="utf-8")
    assert resolve_filesystem_type(tmp_path, mountinfo=mountinfo) == "nfs"


def test_resolution_walks_up_to_an_existing_ancestor(tmp_path: Path) -> None:
    """A ``.sdd/audit`` that has not been created yet still resolves."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"28 1 8:1 / {tmp_path} rw,relatime - cifs //srv/s rw\n", encoding="utf-8")
    missing = tmp_path / "workspace" / ".sdd" / "audit"
    assert resolve_filesystem_type(missing, mountinfo=mountinfo) == "cifs"


def test_resolution_returns_none_on_a_platform_with_neither_source(tmp_path: Path) -> None:
    assert (
        resolve_filesystem_type(
            tmp_path,
            mountinfo=tmp_path / "does-not-exist",
            platform="win32",
        )
        is None
    )


def test_resolution_survives_a_mount_command_that_is_not_there(tmp_path: Path) -> None:
    assert (
        resolve_filesystem_type(
            tmp_path,
            mountinfo=tmp_path / "does-not-exist",
            mount_command=("this-binary-does-not-exist",),
            platform="darwin",
        )
        is None
    )


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_a_local_filesystem_is_ok(tmp_path: Path) -> None:
    result = check_audit_lock_filesystem(tmp_path, has_fcntl=True, filesystem="ext4")
    assert (result.name, result.status) == (CHECK_NAME, "ok")
    assert "ext4" in result.detail


@pytest.mark.parametrize("fstype", sorted(UNRELIABLE_FILESYSTEMS))
def test_every_listed_filesystem_warns_and_says_why(tmp_path: Path, fstype: str) -> None:
    result = check_audit_lock_filesystem(tmp_path, has_fcntl=True, filesystem=fstype)
    assert result.status == "warn"
    assert fstype in result.detail
    assert UNRELIABLE_FILESYSTEMS[fstype] in result.detail
    assert result.remediation


def test_the_type_is_matched_case_insensitively(tmp_path: Path) -> None:
    assert check_audit_lock_filesystem(tmp_path, has_fcntl=True, filesystem="NFS4").status == "warn"


def test_a_platform_without_fcntl_warns_before_it_looks_at_the_filesystem(tmp_path: Path) -> None:
    """The lock is a no-op there whatever the disk is, so the disk is not consulted."""
    result = check_audit_lock_filesystem(tmp_path, has_fcntl=False, filesystem="ext4")
    assert result.status == "warn"
    assert "no-op" in result.detail


def test_an_unresolvable_filesystem_skips_rather_than_guesses(tmp_path: Path) -> None:
    """No answer is reported as no answer, not as a clean bill of health."""
    result = check_audit_lock_filesystem(tmp_path, has_fcntl=True, filesystem="")
    assert result.status == "skip"
    assert str(tmp_path) in result.detail


def test_a_filesystem_not_on_the_advisory_list_reads_ok(tmp_path: Path) -> None:
    """The list is a denylist: an unfamiliar local type is not warned about."""
    result = check_audit_lock_filesystem(tmp_path, has_fcntl=True, filesystem="bcachefs")
    assert result.status == "ok"


def test_the_check_never_fails_the_doctor_run(tmp_path: Path) -> None:
    """An operator on a share has a working install, not a broken one."""
    statuses = {
        check_audit_lock_filesystem(tmp_path, has_fcntl=True, filesystem=fstype).status
        for fstype in [*UNRELIABLE_FILESYSTEMS, "ext4", "apfs", "btrfs", "xfs"]
    }
    statuses.add(check_audit_lock_filesystem(tmp_path, has_fcntl=False, filesystem="ext4").status)
    assert "fail" not in statuses


def test_the_check_is_wired_into_the_doctor_run(tmp_path: Path) -> None:
    """``bernstein doctor`` carries the row; an unwired check helps nobody."""
    import asyncio

    from bernstein.cli.doctor.report import run_all

    results = asyncio.run(run_all(adapter_names=[], provider_names=[], audit_dir=tmp_path / "audit"))
    assert any(r.name == CHECK_NAME for r in results), [r.name for r in results]
