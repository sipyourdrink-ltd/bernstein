"""POSIX regression and lock-pin guards for transitive ``filelock`` (#6759).

Covers the three advisories fixed in ``filelock`` 4.2.1:

* ``CVE-2026-112940`` / ``GHSA-v4x8-3wr5-3wq8`` (POSIX ``ReadWriteLock`` /
  ``AsyncReadWriteLock``): symlink refusal and isolation of SQLite side files
  (``-journal`` / ``-wal`` / ``-shm``) in shared directories, plus WAL-mode lock
  database refusal.
* ``CVE-2026-112907`` / ``GHSA-567q-5qvw-pf92`` (Windows soft-lock oplock) and
  ``CVE-2026-112913`` / ``GHSA-9vhv-jxww-xfm5`` (Windows ``ReadWriteLock``
  SQLite side files): covered by the ``>= 4.2.1`` lock pin below. The POSIX
  runtime tests in this module exercise ``CVE-2026-112940`` only and do not
  claim to reproduce Windows oplock or Windows SQLite side-file behavior.

All runtime checks use only task-owned temporary paths under ``tmp_path``.
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
import sys
import tomllib
from pathlib import Path

import filelock
import pytest
from filelock import FileLock, ReadWriteLock, SoftFileLock, Timeout

REPO_ROOT = Path(__file__).resolve().parents[3]
UV_LOCK = REPO_ROOT / "uv.lock"
PYPROJECT = REPO_ROOT / "pyproject.toml"
PIPX_REQS = REPO_ROOT / ".github" / "requirements" / "pipx-requirements.txt"

_MIN_PATCHED_FILELOCK = (4, 2, 1)
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Exercises POSIX advisory CVE-2026-112940 (GHSA-v4x8-3wr5-3wq8); "
        "Windows advisories CVE-2026-112907 and CVE-2026-112913 are enforced via the >= 4.2.1 lock pin."
    ),
)


def _version_tuple(raw: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", raw.strip())
    assert match is not None, f"unparseable version string: {raw!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def test_lockfile_and_runtime_pin_patched_filelock_without_direct_runtime_dep() -> None:
    """``uv.lock``, pipx requirements, and runtime ``filelock`` stay at >= 4.2.1 and transitive."""
    lock_data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    packages = {pkg["name"]: pkg for pkg in lock_data["package"]}

    assert "filelock" in packages, "filelock missing from uv.lock"
    locked_version = str(packages["filelock"]["version"])
    assert _version_tuple(locked_version) >= _MIN_PATCHED_FILELOCK, (
        f"uv.lock pins vulnerable filelock {locked_version!r} (< 4.2.1)"
    )

    # Confirm the expected transitive parents still declare filelock while
    # bernstein does not promote filelock to a direct runtime dependency.
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    direct_deps = [str(dep).lower() for dep in pyproject["project"]["dependencies"]]
    assert not any(dep.startswith("filelock") for dep in direct_deps), (
        "filelock must remain a transitive dependency, not a direct runtime dependency"
    )
    cachecontrol_filecache = {
        dep["name"] for dep in packages["cachecontrol"].get("optional-dependencies", {}).get("filecache", [])
    }
    assert "filelock" in cachecontrol_filecache
    assert "filelock" in {dep["name"] for dep in packages["datasets"].get("dependencies", [])}
    assert "filelock" in {dep["name"] for dep in packages["huggingface-hub"].get("dependencies", [])}

    # Companion pipx lockfile also stays at or above the patched floor.
    pipx_text = PIPX_REQS.read_text(encoding="utf-8")
    pipx_match = re.search(r"(?m)^filelock==([^\s\\]+)", pipx_text)
    assert pipx_match is not None, "filelock pin missing from .github/requirements/pipx-requirements.txt"
    assert _version_tuple(pipx_match.group(1)) >= _MIN_PATCHED_FILELOCK, (
        f"pipx-requirements.txt pins vulnerable filelock {pipx_match.group(1)!r} (< 4.2.1)"
    )

    # Explicit runtime vendor provenance check.
    runtime_version = getattr(filelock, "__version__", "")
    assert _version_tuple(runtime_version) >= _MIN_PATCHED_FILELOCK, (
        f"imported filelock {runtime_version!r} from {filelock.__file__!r} is below 4.2.1"
    )


@_POSIX_ONLY
def test_posix_read_write_lock_refuses_symlink_and_preserves_sentinel(tmp_path: Path) -> None:
    """CVE-2026-112940: ``ReadWriteLock`` refuses symlinks instead of opening or creating targets."""
    work_dir = tmp_path / "private"
    work_dir.mkdir(mode=0o700)
    os.chmod(work_dir, 0o700)

    # 1. Symlink to an existing task-owned sentinel file must fail with ELOOP and keep bytes intact.
    sentinel = work_dir / "sentinel.txt"
    sentinel_bytes = b"SENTINEL-DO-NOT-OVERWRITE\n"
    sentinel.write_bytes(sentinel_bytes)
    existing_link = work_dir / "existing.lock.db"
    existing_link.symlink_to(sentinel)

    with pytest.raises(OSError) as existing_exc:
        ReadWriteLock(existing_link, timeout=0.5, is_singleton=False)
    assert existing_exc.value.errno == errno.ELOOP, (
        f"expected ELOOP on symlink lock database (filelock {filelock.__version__}), got {existing_exc.value!r}"
    )
    assert sentinel.read_bytes() == sentinel_bytes

    # 2. Dangling symlink must fail with ELOOP and must never create the target file.
    uncreated_target = work_dir / "must_not_be_created.db"
    dangling_link = work_dir / "dangling.lock.db"
    dangling_link.symlink_to(uncreated_target)

    with pytest.raises(OSError) as dangling_exc:
        ReadWriteLock(dangling_link, timeout=0.5, is_singleton=False)
    assert dangling_exc.value.errno == errno.ELOOP, (
        f"expected ELOOP on dangling symlink lock database (filelock {filelock.__version__}), "
        f"got {dangling_exc.value!r}"
    )
    assert not uncreated_target.exists(), "ReadWriteLock followed dangling symlink and created target"


@_POSIX_ONLY
def test_posix_read_write_lock_isolates_sqlite_from_planted_side_files_in_shared_dir(tmp_path: Path) -> None:
    """CVE-2026-112940: in a shared (group-writable) dir, planted SQLite side files are ignored."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    os.chmod(shared_dir, 0o770)

    lock_db = shared_dir / "lock.db"
    seed = ReadWriteLock(lock_db, timeout=1.0, is_singleton=False)
    with seed.write_lock():
        pass
    seed.close()

    # Plant a corrupt SQLite rollback journal header and a non-empty WAL side file
    # beside lock.db inside the group-writable directory. Unpatched filelock (< 4.2.1)
    # opens lock.db directly by shared path so SQLite reads or unlinks these side files.
    planted_wal = shared_dir / "lock.db-wal"
    wal_payload = b"PLANTED-WAL-POISON" * 32
    planted_wal.write_bytes(wal_payload)

    planted_journal = shared_dir / "lock.db-journal"
    journal_payload = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + (b"\x00" * 504)
    planted_journal.write_bytes(journal_payload)

    rw = ReadWriteLock(lock_db, timeout=1.0, is_singleton=False)
    try:
        with rw.write_lock():
            pass
        with rw.read_lock():
            pass
    finally:
        rw.close()

    assert planted_wal.exists() and planted_wal.read_bytes() == wal_payload
    assert planted_journal.exists() and planted_journal.read_bytes() == journal_payload


@_POSIX_ONLY
def test_posix_read_write_lock_refuses_wal_mode_database(tmp_path: Path) -> None:
    """CVE-2026-112940: ``ReadWriteLock`` rejects an existing WAL-mode SQLite database."""
    private_dir = tmp_path / "private_wal"
    private_dir.mkdir(mode=0o700)
    os.chmod(private_dir, 0o700)

    wal_db = private_dir / "wal_mode.db"
    con = sqlite3.connect(wal_db)
    try:
        con.execute("PRAGMA journal_mode=WAL;").close()
        con.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY);").close()
        con.commit()
    finally:
        con.close()

    with pytest.raises(ValueError, match="WAL-mode"):
        ReadWriteLock(wal_db, timeout=0.5, is_singleton=False)


def test_private_directory_lock_acquisition_and_api_compatibility(tmp_path: Path) -> None:
    """Legitimate private-directory locks keep constructor, timeout, and context-manager behavior."""
    private_dir = tmp_path / "private_ok"
    private_dir.mkdir(mode=0o700)
    if sys.platform != "win32":
        os.chmod(private_dir, 0o700)

    # ReadWriteLock: context managers, reentrancy, contention Timeout, upgrade/downgrade refusal.
    rw_path = private_dir / "rw.db"
    rw_a = ReadWriteLock(rw_path, timeout=0.2, blocking=True, is_singleton=False)
    rw_b = ReadWriteLock(rw_path, timeout=0.05, blocking=False, is_singleton=False)
    try:
        with rw_a.read_lock():
            with rw_a.read_lock():
                with rw_b.read_lock():
                    pass
                with pytest.raises(Timeout), rw_b.write_lock(blocking=False):
                    pass
            with pytest.raises(RuntimeError), rw_a.write_lock():
                pass

        with rw_a.write_lock():
            with rw_a.write_lock():
                with pytest.raises(Timeout), rw_b.read_lock(blocking=False):
                    pass
                with pytest.raises(Timeout), rw_b.write_lock(blocking=False):
                    pass
            with pytest.raises(RuntimeError), rw_a.read_lock():
                pass
    finally:
        rw_b.close()
        rw_a.close()

    # FileLock and SoftFileLock context managers and contention Timeout.
    for lock_cls, filename in ((FileLock, "native.lock"), (SoftFileLock, "soft.lock")):
        lock_path = private_dir / filename
        holder = lock_cls(str(lock_path), timeout=0.5)
        contender = lock_cls(str(lock_path), timeout=0.05, thread_local=False)
        with holder:
            with pytest.raises(Timeout):
                contender.acquire(timeout=0.05)
