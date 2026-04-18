"""Disaster recovery: backup and restore .sdd/ state.

Creates compressed tarballs of all persistent .sdd/ subdirectories,
including the durable subset of ``runtime/`` (WAL, file locks, session
state, team roster, task graph, budgets, incident history), and excluding
only truly transient data — process-lifecycle markers (``runtime/pids/``,
``runtime/signals/``, ``runtime/draining/``, ``*.kill`` files), liveness
streams (``runtime/heartbeats/``, ``runtime/hooks/``), and log files
(``*.log``, ``*.log.1``, ``access.jsonl*``, ``retrospective.md``,
``summary.md``).  Worktrees, cached research, and per-role defaults are
also excluded since they are derivable or workspace-specific.

Supports local file destinations and optional encryption via symmetric
Fernet cipher.

Usage::

    bernstein backup --to ./backup.tar.gz
    bernstein backup --to ./backup.tar.gz --encrypt
    bernstein restore --from ./backup.tar.gz
    bernstein restore --from ./backup.tar.gz --decrypt
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import tarfile
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Top-level directories included in backup (persistent state only).
#
# ``runtime/`` is included — it hosts live state (WAL, file locks,
# sessions, team roster, task graph, budgets) that makes the difference
# between a warm restart and a cold start.  Transient contents inside
# ``runtime/`` are filtered out by ``_EXCLUDE_PATTERNS`` below.
_BACKUP_DIRS = (
    "backlog/open",
    "backlog/done",
    "backlog/closed",
    "backlog/deferred",
    "backlog/manual",
    "metrics",
    "traces",
    "memory",
    "sessions",
    "decisions",
    "docs",
    "config",
    "archive",
    "agents",
    "index",
    "caching",
    "models",
    "audit",
    "runs",
    "runtime",
)

# Top-level directories excluded outright (ephemeral or derivable).
#
# - ``logs``        : rotated log files, rebuildable from observability.
# - ``worktrees``   : per-branch git worktrees, rebuilt on demand.
# - ``signals``     : top-level signal inbox (distinct from
#   ``runtime/signals``); both are in-flight only.
# - ``debug``       : ad-hoc debug dumps.
# - ``research``    : cached retrieval results, regenerable.
# - ``default``     : per-role default fallback workspaces.
# - ``upgrades``    : in-progress upgrade staging.
_EXCLUDE_DIRS = (
    "logs",
    "worktrees",
    "signals",
    "debug",
    "research",
    "default",
    "upgrades",
)

# Path globs (relative to ``.sdd/``) that should be excluded even when
# their parent directory is in ``_BACKUP_DIRS``.  These cover transient
# data inside ``runtime/`` that would otherwise be pulled into backups:
#
# - ``runtime/pids/*``       : PID files for live processes.
# - ``runtime/signals/*``    : in-flight control signals.
# - ``runtime/heartbeats/*`` : agent liveness beacons (seconds-old).
# - ``runtime/hooks/*``      : per-agent hook streams (tail-only).
# - ``runtime/draining/*``   : shutdown-in-progress markers.
# - ``runtime/gates/*``      : ephemeral gate acquisition markers.
# - ``runtime/completed/*``  : per-spawn completion stubs (replayable
#   from backlog + WAL).
# - ``runtime/*.log``        : log files (rotated and non-rotated).
# - ``runtime/*.log.[0-9]*`` : rotated log suffixes.
# - ``runtime/*.kill``       : agent kill markers (consumed once).
# - ``runtime/*.pid``        : loose PID files at runtime root.
# - ``runtime/access.jsonl*``: HTTP access log (can grow unbounded).
# - ``runtime/retrospective.md`` / ``runtime/summary.md`` : derived
#   human-readable reports regenerated each run.
_EXCLUDE_PATTERNS = (
    "runtime/pids/*",
    "runtime/signals/*",
    "runtime/heartbeats/*",
    "runtime/hooks/*",
    "runtime/draining/*",
    "runtime/gates/*",
    "runtime/completed/*",
    "runtime/*.log",
    "runtime/*.log.[0-9]*",
    "runtime/*.kill",
    "runtime/*.pid",
    "runtime/access.jsonl*",
    "runtime/retrospective.md",
    "runtime/summary.md",
)

_MANIFEST_FILE = "manifest.json"


def _is_excluded(rel_path: str) -> bool:
    """Return True if *rel_path* (posix-style, relative to ``.sdd/``) matches
    any entry in :data:`_EXCLUDE_PATTERNS`.
    """
    return any(fnmatch.fnmatchcase(rel_path, pat) for pat in _EXCLUDE_PATTERNS)


_PBKDF2_SALT_LEN = 16
_PBKDF2_ITERATIONS = 600_000


def _get_crypto(
    encrypt: bool,
    password: str | None = None,
    salt: bytes | None = None,
) -> tuple[Fernet | None, bytes | None]:
    """Return a (Fernet cipher, salt) pair for encryption, or (None, None).

    When *password* is given, derives the key via PBKDF2-SHA256.  If *salt*
    is not provided a fresh random salt is generated (use for encryption);
    pass the stored salt back for decryption.
    """
    if not encrypt:
        return None, None

    import base64
    import os

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if password:
        if salt is None:
            salt = os.urandom(_PBKDF2_SALT_LEN)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        key = kdf.derive(password.encode())
        key_b64 = base64.urlsafe_b64encode(key)
    else:
        key_b64 = None  # type: ignore[assignment]

    return Fernet(key_b64 if key_b64 else Fernet.generate_key()), salt  # type: ignore[arg-type]


def backup_sdd(
    sdd_path: Path,
    dest: Path,
    *,
    encrypt: bool = False,
    password: str | None = None,
) -> dict[str, str]:
    """Backup persistent .sdd/ state to *dest*.

    Args:
        sdd_path: Path to the .sdd/ directory.
        dest: Destination tar.gz path.
        encrypt: Whether to encrypt the backup.
        password: Password for encryption (optional).

    Returns:
        Dict with ``path``, ``size_bytes``, ``file_count``, ``sha256``.

    Raises:
        ValueError: If ``encrypt`` is True but no ``password`` is provided.
            Without a password, encryption would derive an ephemeral random
            Fernet key that is never persisted, making restore impossible.
    """
    if encrypt and not password:
        raise ValueError("encryption requires a password")

    sdd_path = sdd_path.resolve()
    if not sdd_path.is_dir():
        raise FileNotFoundError(f".sdd/ directory not found: {sdd_path}")

    manifest: dict[str, object] = {
        "created_at": time.time(),
        "included_dirs": list(_BACKUP_DIRS),
        "excluded_dirs": list(_EXCLUDE_DIRS),
        "excluded_patterns": list(_EXCLUDE_PATTERNS),
    }
    file_count = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Write manifest
        import json

        manifest_path = tmp_path / _MANIFEST_FILE
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        file_count += 1

        # Copy included directories, skipping per-pattern excludes.
        for rel_dir in _BACKUP_DIRS:
            src_dir = sdd_path / rel_dir
            dst_dir = tmp_path / rel_dir
            if not src_dir.exists():
                continue

            dst_dir.mkdir(parents=True, exist_ok=True)
            for item in src_dir.rglob("*"):
                if not item.is_file():
                    continue
                rel = item.relative_to(sdd_path)
                if _is_excluded(rel.as_posix()):
                    continue
                dst_file = tmp_path / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                dst_file.write_bytes(item.read_bytes())
                file_count += 1

        # Create tar.gz
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            for item in tmp_path.iterdir():
                tar.add(item, arcname=item.name)

    # If encryption requested, encrypt the tarball
    final_dest = dest
    if encrypt:
        fernet, salt = _get_crypto(True, password)
        assert fernet is not None

        tar_data = dest.read_bytes()
        encrypted = fernet.encrypt(tar_data)
        encrypted_dest = dest.with_suffix(dest.suffix + ".enc")
        # Prepend salt (if password-derived) so restore can re-derive the key
        prefix = salt if salt is not None else b""
        encrypted_dest.write_bytes(prefix + encrypted)
        final_dest = encrypted_dest

        # Delete unencrypted tarball
        dest.unlink(missing_ok=True)

    sha256 = hashlib.sha256(final_dest.read_bytes()).hexdigest()
    size = final_dest.stat().st_size

    return {
        "path": str(final_dest),
        "size_bytes": str(size),
        "file_count": str(file_count),
        "sha256": sha256,
    }


def restore_sdd(
    source: Path,
    sdd_path: Path,
    *,
    decrypt: bool = False,
    password: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Restore .sdd/ state from *source*.

    Args:
        source: Source tar.gz (or .tar.gz.enc) path.
        sdd_path: Path to the .sdd/ directory to restore into.
        decrypt: Whether to decrypt the backup.
        password: Password for decryption (optional).
        dry_run: If True, only list contents without extracting.

    Returns:
        Dict with ``files_restored``, ``source``, ``sha256``.
    """
    if not source.exists():
        raise FileNotFoundError(f"Backup file not found: {source}")

    # Handle encrypted file
    data = source.read_bytes()
    if decrypt:
        # If password-derived, the first _PBKDF2_SALT_LEN bytes are the salt
        salt: bytes | None = None
        if password and len(data) > _PBKDF2_SALT_LEN:
            salt = data[:_PBKDF2_SALT_LEN]
            data = data[_PBKDF2_SALT_LEN:]
        fernet, _ = _get_crypto(True, password, salt=salt)
        assert fernet is not None
        data = fernet.decrypt(data)

    if dry_run:
        # Open from memory if decrypted
        import io

        if decrypt:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                # Read-only: getnames() lists entries without extraction.
                names = tar.getnames()
                return {
                    "files_restored": str(len(names)),
                    "source": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "files": "\n".join(names[:50]),
                }
        else:
            with tarfile.open(source, "r:gz") as tar:
                # Read-only: getnames() lists entries without extraction.
                names = tar.getnames()
                return {
                    "files_restored": str(len(names)),
                    "source": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "files": "\n".join(names[:50]),
                }

    # Actual restore
    sdd_path = sdd_path.resolve()
    sdd_path.mkdir(parents=True, exist_ok=True)

    import io

    if decrypt:
        # In-memory BytesIO — no OS file descriptor to leak.
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            # filter="data" (Python 3.12+) blocks absolute paths, ".."
            # components, and special file types — mitigating path traversal.
            tar.extractall(path=sdd_path, filter="data")
    else:
        # Bind the source fd to a context manager so it is closed even if
        # tarfile.open raises.  tarfile does not own fileobj-supplied fds.
        with source.open("rb") as fh, tarfile.open(fileobj=fh, mode="r:*") as tar:
            # filter="data" (Python 3.12+) blocks absolute paths, ".."
            # components, and special file types — mitigating path traversal.
            tar.extractall(path=sdd_path, filter="data")

    # Count restored files
    restored = sum(1 for _ in sdd_path.rglob("*") if _.is_file())
    sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    return {
        "files_restored": str(restored),
        "source": str(source),
        "sha256": sha256,
    }
