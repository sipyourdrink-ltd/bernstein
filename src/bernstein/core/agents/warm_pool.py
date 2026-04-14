"""Agent warm pool for fast re-spawning (gh-362).

Pre-provisions pool slots with worktrees and optional MCP processes so that
agent spawning can skip the 5-15s cold-start overhead.  Slots are claimed
FIFO by role and automatically expired after a configurable TTL.

Usage::

    config = WarmPoolConfig(max_slots=3, slot_ttl_seconds=300, roles=["backend", "qa"])
    pool = WarmPool(config)
    pool.add_slot(PoolSlot(slot_id="s1", role="backend", worktree_path="/tmp/wt1", created_at=time.time()))
    slot = pool.claim_slot("backend")
    if slot:
        # ... use slot.worktree_path ...
        pool.release_slot(slot.slot_id)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolSlot:
    """A single pre-provisioned slot in the warm pool.

    Attributes:
        slot_id: Unique identifier for this slot.
        role: Agent role this slot is provisioned for.
        worktree_path: Filesystem path to the pre-created worktree.
        created_at: Unix timestamp when the slot was created.
        status: Current lifecycle state of the slot.
        mcp_pid: PID of the pre-started MCP server process, if any.
    """

    slot_id: str
    role: str
    worktree_path: str
    created_at: float
    status: Literal["ready", "claimed", "expired"] = "ready"
    mcp_pid: int | None = None


@dataclass(frozen=True)
class WarmPoolConfig:
    """Configuration for the agent warm pool.

    Attributes:
        max_slots: Maximum number of slots to maintain in the pool.
        slot_ttl_seconds: Seconds before an unclaimed slot expires.
        roles: Default roles to pre-provision slots for.
    """

    max_slots: int = 3
    slot_ttl_seconds: float = 300.0
    roles: list[str] = field(default_factory=lambda: list[str]())


# ---------------------------------------------------------------------------
# Pool manager
# ---------------------------------------------------------------------------


class WarmPool:
    """Manages a pool of pre-provisioned agent slots for fast re-spawning.

    Slots are added externally (by the spawner or a background fill task),
    claimed FIFO by role, and expired when they exceed the configured TTL.

    Args:
        config: Pool configuration.
    """

    def __init__(self, config: WarmPoolConfig) -> None:
        self._config = config
        self._slots: list[PoolSlot] = []

    @property
    def config(self) -> WarmPoolConfig:
        """Return the pool configuration."""
        return self._config

    def add_slot(self, slot: PoolSlot) -> None:
        """Add a pre-provisioned slot to the pool.

        Slots beyond ``max_slots`` are silently ignored.

        Args:
            slot: The slot to add.
        """
        if len(self._slots) >= self._config.max_slots:
            logger.debug(
                "Warm pool: ignoring add_slot(%s), pool at capacity (%d/%d)",
                slot.slot_id,
                len(self._slots),
                self._config.max_slots,
            )
            return
        self._slots.append(slot)
        logger.debug(
            "Warm pool: added slot %s role=%s (%d/%d)",
            slot.slot_id,
            slot.role,
            len(self._slots),
            self._config.max_slots,
        )

    def claim_slot(self, role: str) -> PoolSlot | None:
        """Claim the oldest ready slot matching the given role.

        Returns the claimed slot with its status set to ``"claimed"``,
        or ``None`` if no matching ready slot is available.  Operates
        in FIFO order.

        Args:
            role: The agent role to match.

        Returns:
            The claimed PoolSlot, or None.
        """
        for idx, slot in enumerate(self._slots):
            if slot.status == "ready" and slot.role == role:
                claimed = PoolSlot(
                    slot_id=slot.slot_id,
                    role=slot.role,
                    worktree_path=slot.worktree_path,
                    created_at=slot.created_at,
                    status="claimed",
                    mcp_pid=slot.mcp_pid,
                )
                self._slots[idx] = claimed
                logger.info(
                    "Warm pool: claimed slot %s for role=%s",
                    claimed.slot_id,
                    role,
                )
                return claimed
        return None

    def release_slot(self, slot_id: str) -> None:
        """Mark a slot as expired by its ID.

        Args:
            slot_id: The ID of the slot to release.
        """
        for idx, slot in enumerate(self._slots):
            if slot.slot_id == slot_id:
                expired = PoolSlot(
                    slot_id=slot.slot_id,
                    role=slot.role,
                    worktree_path=slot.worktree_path,
                    created_at=slot.created_at,
                    status="expired",
                    mcp_pid=slot.mcp_pid,
                )
                self._slots[idx] = expired
                logger.debug("Warm pool: released slot %s", slot_id)
                return

    def expire_stale(self, now: float | None = None) -> None:
        """Expire ready slots that have exceeded the configured TTL.

        Only affects slots with status ``"ready"``.  Already claimed or
        expired slots are left untouched.

        Args:
            now: Current Unix timestamp.  Defaults to ``time.time()``.
        """
        current = now if now is not None else time.time()
        ttl = self._config.slot_ttl_seconds
        for idx, slot in enumerate(self._slots):
            if slot.status == "ready" and (current - slot.created_at) > ttl:
                expired = PoolSlot(
                    slot_id=slot.slot_id,
                    role=slot.role,
                    worktree_path=slot.worktree_path,
                    created_at=slot.created_at,
                    status="expired",
                    mcp_pid=slot.mcp_pid,
                )
                self._slots[idx] = expired
                logger.debug(
                    "Warm pool: expired stale slot %s (age=%.1fs, ttl=%.1fs)",
                    slot.slot_id,
                    current - slot.created_at,
                    ttl,
                )

    def stats(self) -> dict[str, int]:
        """Return pool statistics.

        Returns:
            Dictionary with keys: ready, claimed, expired, total.
        """
        ready = sum(1 for s in self._slots if s.status == "ready")
        claimed = sum(1 for s in self._slots if s.status == "claimed")
        expired = sum(1 for s in self._slots if s.status == "expired")
        return {
            "ready": ready,
            "claimed": claimed,
            "expired": expired,
            "total": len(self._slots),
        }

    def available_roles(self) -> list[str]:
        """Return deduplicated list of roles with at least one ready slot.

        Returns:
            Sorted list of role names.
        """
        roles: list[str] = []
        seen: set[str] = set()
        for slot in self._slots:
            if slot.status == "ready" and slot.role not in seen:
                roles.append(slot.role)
                seen.add(slot.role)
        return sorted(roles)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _parse_warm_pool_section(section: dict[str, Any]) -> WarmPoolConfig:
    """Parse a warm_pool config section dict into WarmPoolConfig."""
    max_slots_raw: Any = section.get("max_slots", 3)
    slot_ttl_raw: Any = section.get("slot_ttl_seconds", 300.0)
    roles_raw: Any = section.get("roles", [])
    max_slots: int = max_slots_raw if isinstance(max_slots_raw, int) and not isinstance(max_slots_raw, bool) else 3
    slot_ttl: float = (
        float(slot_ttl_raw) if isinstance(slot_ttl_raw, (int, float)) and not isinstance(slot_ttl_raw, bool) else 300.0
    )
    if not isinstance(roles_raw, list):
        roles_raw = []
    return WarmPoolConfig(
        max_slots=max_slots,
        slot_ttl_seconds=float(slot_ttl),
        roles=[str(r) for r in cast("list[Any]", roles_raw)],
    )


def load_warm_pool_config(yaml_path: Path | None = None) -> WarmPoolConfig:
    """Load warm pool configuration from a YAML file.

    Reads the ``warm_pool:`` section from ``bernstein.yaml``.  Falls back
    to defaults if the file is missing, unparseable, or lacks the section.

    Expected YAML structure::

        warm_pool:
          max_slots: 5
          slot_ttl_seconds: 600
          roles:
            - backend
            - qa

    Args:
        yaml_path: Path to config file.  When ``None``, searches
            ``bernstein.yaml`` in the current directory and
            ``~/.bernstein/bernstein.yaml``.

    Returns:
        WarmPoolConfig populated from the file, or defaults.
    """
    try:
        import yaml
    except ImportError:
        return WarmPoolConfig()

    candidates: list[Path] = []
    if yaml_path is not None:
        candidates.append(yaml_path)
    else:
        candidates.append(Path("bernstein.yaml"))
        candidates.append(Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            section_raw: Any = cast("dict[str, Any]", data).get("warm_pool")
            if not isinstance(section_raw, dict):
                continue
            return _parse_warm_pool_section(cast("dict[str, Any]", section_raw))
        except Exception:
            logger.debug("Warm pool: failed to parse config from %s", path)
            continue

    return WarmPoolConfig()
