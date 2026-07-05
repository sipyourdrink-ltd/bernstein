"""``teams.lock`` - pinned team manifests with lineage receipts (issue #2248).

Same physical shape as the catalog-extended ``skills.lock``
(:mod:`bernstein.core.skills.catalog.lockfile`): a deterministic
hand-rolled TOML file with one ``[[teams]]`` row per pinned manifest and
a ``[[lineage_receipt]]`` array recording every chain-head change. The
receipt machinery, the coarse cross-worktree file lock, and the atomic
tmp-file swap are reused from the skills lockfile rather than
re-implemented, so both lockfiles keep identical durability and
determinism guarantees.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from bernstein.core.skills.catalog.lockfile import (
    RECEIPT_ADOPT,
    RECEIPT_INSTALL,
    RECEIPT_PIN,
    LineageReceipt,
    _acquire_lock,
    _release_lock,
    worktree_id_for,
)
from bernstein.core.skills.lifecycle import _toml_quote

if TYPE_CHECKING:
    from pathlib import Path

#: Filename of the team manifest lock; sibling of ``skills.lock`` at the
#: project root.
TEAMS_LOCK_FILENAME = "teams.lock"

#: Genesis chain head used before any audit event exists.
GENESIS_CHAIN_HEAD = "0" * 64


@dataclass(frozen=True)
class TeamLockEntry:
    """One ``[[teams]]`` row in ``teams.lock``.

    Attributes:
        name: Manifest name.
        version: Manifest version at pin time.
        manifest_digest: SHA-256 of the manifest's canonical serialization.
        source: Where the manifest was resolved from (path or ``builtin``).
        install_id: Unique id tying the row to its audit event.
        chain_head: Audit-chain head after the pin event was appended.
        installed_at: ISO-8601 UTC timestamp of the pin.
    """

    name: str
    version: str
    manifest_digest: str
    source: str
    install_id: str
    chain_head: str
    installed_at: str


@dataclass(frozen=True)
class TeamLockState:
    """Parsed view of ``teams.lock``."""

    teams: list[TeamLockEntry] = field(default_factory=list)
    receipts: list[LineageReceipt] = field(default_factory=list)

    def find(self, name: str) -> TeamLockEntry | None:
        """Return the lock row for *name* or ``None``."""
        for row in self.teams:
            if row.name == name:
                return row
        return None

    def digest(self) -> str:
        """Return a deterministic digest of the pinned team rows.

        Two worktrees on the same chain head produce identical digests;
        receipts and volatile fields (install ids, timestamps) are
        excluded.
        """
        canonical = json.dumps(
            [
                {
                    "name": row.name,
                    "version": row.version,
                    "manifest_digest": row.manifest_digest,
                    "chain_head": row.chain_head,
                }
                for row in sorted(self.teams, key=lambda row: row.name)
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class _MissingField(Exception):
    """Internal sentinel for malformed lockfile rows."""


def _required_str(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise _MissingField(key)
    return value


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def read_state(lockfile_path: Path) -> TeamLockState:
    """Parse ``teams.lock``, returning an empty state on any parse error."""
    import tomllib

    if not lockfile_path.is_file():
        return TeamLockState()
    try:
        text = lockfile_path.read_text(encoding="utf-8")
    except OSError:
        return TeamLockState()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return TeamLockState()

    teams: list[TeamLockEntry] = []
    for raw in cast("list[object]", data.get("teams", [])):
        if not isinstance(raw, dict):
            continue
        row = cast("dict[str, object]", raw)
        try:
            teams.append(
                TeamLockEntry(
                    name=_required_str(row, "name"),
                    version=_required_str(row, "version"),
                    manifest_digest=_required_str(row, "manifest_digest"),
                    source=_required_str(row, "source"),
                    install_id=_required_str(row, "install_id"),
                    chain_head=_required_str(row, "chain_head"),
                    installed_at=_required_str(row, "installed_at"),
                )
            )
        except _MissingField:
            continue

    receipts: list[LineageReceipt] = []
    for raw in cast("list[object]", data.get("lineage_receipt", [])):
        if not isinstance(raw, dict):
            continue
        row = cast("dict[str, object]", raw)
        try:
            receipts.append(
                LineageReceipt(
                    worktree_id=_required_str(row, "worktree_id"),
                    action=_required_str(row, "action"),
                    entry_id=_required_str(row, "entry_id"),
                    from_chain_head=_required_str(row, "from_chain_head"),
                    to_chain_head=_required_str(row, "to_chain_head"),
                    manifest_sha256=_required_str(row, "manifest_sha256"),
                    timestamp=_required_str(row, "timestamp"),
                )
            )
        except _MissingField:
            continue

    return TeamLockState(teams=teams, receipts=receipts)


def write_state(lockfile_path: Path, state: TeamLockState) -> None:
    """Write ``teams.lock`` atomically with deterministic formatting."""
    lines: list[str] = [
        "# bernstein teams lock file - regenerated by `bernstein team` commands.",
        "# Do not edit by hand.",
        "",
    ]
    for row in sorted(state.teams, key=lambda row: row.name):
        lines.extend(
            (
                "[[teams]]",
                f"name = {_toml_quote(row.name)}",
                f"version = {_toml_quote(row.version)}",
                f"manifest_digest = {_toml_quote(row.manifest_digest)}",
                f"source = {_toml_quote(row.source)}",
                f"install_id = {_toml_quote(row.install_id)}",
                f"chain_head = {_toml_quote(row.chain_head)}",
                f"installed_at = {_toml_quote(row.installed_at)}",
                "",
            )
        )
    for receipt in sorted(
        state.receipts,
        key=lambda r: (r.timestamp, r.entry_id, r.worktree_id),
    ):
        lines.extend(
            (
                "[[lineage_receipt]]",
                f"worktree_id = {_toml_quote(receipt.worktree_id)}",
                f"action = {_toml_quote(receipt.action)}",
                f"entry_id = {_toml_quote(receipt.entry_id)}",
                f"from_chain_head = {_toml_quote(receipt.from_chain_head)}",
                f"to_chain_head = {_toml_quote(receipt.to_chain_head)}",
                f"manifest_sha256 = {_toml_quote(receipt.manifest_sha256)}",
                f"timestamp = {_toml_quote(receipt.timestamp)}",
                "",
            )
        )

    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lockfile_path.with_suffix(lockfile_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    # ``Path.replace`` is atomic on POSIX and on Windows (since 3.3).
    tmp.replace(lockfile_path)


def upsert_team_pin(
    lockfile_path: Path,
    entry: TeamLockEntry,
    *,
    workdir: Path,
    from_chain_head: str = GENESIS_CHAIN_HEAD,
) -> TeamLockState:
    """Insert or replace a team row, emitting a lineage receipt.

    Receipt semantics mirror the skills catalog lockfile: first pin emits
    an install receipt, a rewrite on the same chain head emits a pin
    receipt, a rewrite on a new chain head emits an adopt receipt.

    Args:
        lockfile_path: Path to ``teams.lock``.
        entry: The row to write.
        workdir: Worktree root, used to derive the receipt's worktree id.
        from_chain_head: Chain head visible before the very first pin.

    Returns:
        The post-write :class:`TeamLockState`.
    """
    guard = _acquire_lock(lockfile_path)
    try:
        state = read_state(lockfile_path)
        prior = state.find(entry.name)
        teams = [row for row in state.teams if row.name != entry.name]
        teams.append(entry)

        if prior is None:
            action = RECEIPT_INSTALL
            from_head = from_chain_head
        elif prior.chain_head == entry.chain_head:
            action = RECEIPT_PIN
            from_head = prior.chain_head
        else:
            action = RECEIPT_ADOPT
            from_head = prior.chain_head

        receipt = LineageReceipt(
            worktree_id=worktree_id_for(workdir),
            action=action,
            entry_id=entry.name,
            from_chain_head=from_head,
            to_chain_head=entry.chain_head,
            manifest_sha256=entry.manifest_digest,
            timestamp=_utc_now(),
        )
        new_state = TeamLockState(teams=teams, receipts=[*state.receipts, receipt])
        write_state(lockfile_path, new_state)
        return new_state
    finally:
        _release_lock(guard)


def fresh_install_id() -> str:
    """Return a new install identifier."""
    return uuid.uuid4().hex


__all__ = [
    "GENESIS_CHAIN_HEAD",
    "TEAMS_LOCK_FILENAME",
    "TeamLockEntry",
    "TeamLockState",
    "fresh_install_id",
    "read_state",
    "upsert_team_pin",
    "write_state",
]
