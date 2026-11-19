"""Persistent claim-admission state for one configured single-process TaskStore.

This is operator-owned control state, not a worker lease, global execution stop,
cryptographic receipt or a boundary against another process running as its owner.
The TaskStore serializes reads and transitions with the same lock as claims.
"""

from __future__ import annotations

import math
import os
import sqlite3
import stat
import time
from contextlib import closing
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from pathlib import Path


class ClaimControlError(ValueError):
    """Missing, inconsistent or unavailable control state refuses admission."""


type ClaimAction = Literal["enroll", "halt", "resume"]


class ClaimAdmissionStatus(TypedDict):
    scope: str
    controller_id: str
    halted: bool
    revision: int
    actor: str
    reason: str


class ClaimAdmissionController:
    """An explicitly configured controller bound to one canonical store path.

    The parent must exist and be private to the operator. Missing state is not
    automatically initialized. Enrollment is explicit and starts halted; corrupt
    or mismatched history requires separate operator recovery, never resume.
    """

    def __init__(self, path: Path, controller_id: str, store_path: Path) -> None:
        if not path.is_absolute() or not controller_id.strip() or len(controller_id) > 200:
            raise ClaimControlError("Claim controller requires an absolute path and a bounded controller id")
        self.path = path
        self.controller_id = controller_id
        self.store_identity = str(store_path.resolve())

    def _check_layout(self, *, existing: bool = True) -> None:
        parent = self.path.parent
        try:
            if parent.is_symlink() or not parent.is_dir():
                raise ClaimControlError("Claim controller parent must be an operator directory")
            info = parent.stat()
            if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise ClaimControlError("Claim controller parent must be private to its operator")
            if existing:
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ClaimControlError("Claim controller must be a regular file")
                if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                    raise ClaimControlError("Claim controller file must be private to its operator")
        except OSError as exc:
            raise ClaimControlError("Claim controller state is missing or unavailable") from exc

    def _read(self, connection: sqlite3.Connection) -> ClaimAdmissionStatus:
        metadata = connection.execute("SELECT version, controller_id, store_identity FROM scope").fetchall()
        if metadata != [(1, self.controller_id, self.store_identity)]:
            raise ClaimControlError("Claim controller scope does not match this store")
        rows = connection.execute(
            "SELECT revision, action, actor, reason, created_at FROM transitions ORDER BY revision"
        ).fetchall()
        if not rows:
            raise ClaimControlError("Claim controller history is empty")
        for index, row in enumerate(rows, 1):
            revision, action, actor, reason, created_at = row
            if (
                revision != index
                or type(revision) is not int
                or action not in (("enroll",) if index == 1 else ("halt", "resume"))
                or not isinstance(actor, str)
                or not actor.strip()
                or len(actor) > 300
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 2000
                or not isinstance(created_at, (int, float))
                or not math.isfinite(created_at)
                or created_at <= 0
            ):
                raise ClaimControlError("Claim controller history is malformed")
        revision, action, actor, reason, _ = rows[-1]
        return {
            "scope": "task-claim-admission",
            "controller_id": self.controller_id,
            "halted": action != "resume",
            "revision": revision,
            "actor": actor,
            "reason": reason,
        }

    def snapshot(self) -> ClaimAdmissionStatus:
        self._check_layout()
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
                return self._read(connection)
        except (sqlite3.Error, OverflowError) as exc:
            raise ClaimControlError("Claim controller state is unreadable or malformed") from exc

    def transition(
        self, action: ClaimAction, *, actor: str, reason: str, expected_revision: int
    ) -> ClaimAdmissionStatus:
        if action not in ("enroll", "halt", "resume"):
            raise ClaimControlError("Unknown claim controller transition")
        if not actor.strip() or len(actor) > 300 or not reason.strip() or len(reason) > 2000:
            raise ClaimControlError("A bounded operator identity and nonempty reason are required")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ClaimControlError("An explicit current revision is required")
        if action == "enroll" and expected_revision != 0:
            raise ClaimControlError("Enrollment requires initial revision zero")
        self._check_layout(existing=action != "enroll")
        try:
            if action == "enroll":
                # Exclusive creation preserves existing or corrupt evidence.
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                if action == "enroll":
                    connection.execute("CREATE TABLE scope (version INTEGER, controller_id TEXT, store_identity TEXT)")
                    connection.execute(
                        "INSERT INTO scope VALUES (?, ?, ?)", (1, self.controller_id, self.store_identity)
                    )
                    connection.execute(
                        "CREATE TABLE transitions (revision INTEGER PRIMARY KEY, action TEXT, "
                        "actor TEXT, reason TEXT, created_at REAL)"
                    )
                    revision = 1
                else:
                    previous = self._read(connection)["revision"]
                    if previous != expected_revision:
                        raise ClaimControlError("Claim control revision changed; reload before another action")
                    revision = previous + 1
                connection.execute(
                    "INSERT INTO transitions VALUES (?, ?, ?, ?, ?)", (revision, action, actor, reason, time.time())
                )
                result = self._read(connection)
                connection.commit()
            # Persist the initial directory entry as well as SQLite's pages.
            if action == "enroll" and os.name == "posix":
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return result
        except (OSError, sqlite3.Error, OverflowError) as exc:
            raise ClaimControlError("Claim controller transition failed; preserve state for operator recovery") from exc
