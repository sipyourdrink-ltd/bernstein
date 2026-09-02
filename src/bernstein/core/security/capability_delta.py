from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs

if TYPE_CHECKING:
    from bernstein.core.security.permissions import AgentPermissions


class GrantDirection(str, Enum):  # noqa: UP042
    WIDENING = "WIDENING"
    NARROWING = "NARROWING"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class GrantChange:
    path: str
    direction: GrantDirection
    axis: str
    old_value: str | None
    new_value: str | None


@dataclass(frozen=True)
class GrantDelta:
    role: str
    run_id: str
    changes: tuple[GrantChange, ...]
    timestamp_ns: int

    @property
    def delta_hash(self) -> str:
        body = self.to_dict()
        digest = hashlib.sha256(canonicalize_jcs(body)).hexdigest()
        return f"sha256:{digest}"

    @property
    def is_widening(self) -> bool:
        return any(c.direction == GrantDirection.WIDENING for c in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "run_id": self.run_id,
            "changes": [
                {
                    "path": c.path,
                    "direction": c.direction.value,
                    "axis": c.axis,
                    "old_value": c.old_value,
                    "new_value": c.new_value,
                }
                for c in self.changes
            ],
            "timestamp_ns": self.timestamp_ns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrantDelta:
        changes = []
        for c in data["changes"]:
            changes.append(
                GrantChange(
                    path=c["path"],
                    direction=GrantDirection(c["direction"]),
                    axis=c["axis"],
                    old_value=c["old_value"],
                    new_value=c["new_value"],
                )
            )
        return cls(
            role=data.get("role", ""),
            run_id=data.get("run_id", ""),
            changes=tuple(changes),
            timestamp_ns=data.get("timestamp_ns", 0),
        )


def compute_grant_delta(
    old: AgentPermissions,
    new: AgentPermissions,
    role: str,
    run_id: str,
    timestamp_ns: int = 0,
) -> GrantDelta:
    changes: list[GrantChange] = []

    old_allowed = set(old.allowed_paths)
    new_allowed = set(new.allowed_paths)

    for path in new_allowed:
        if path not in old_allowed:
            changes.append(
                GrantChange(
                    path=path,
                    direction=GrantDirection.WIDENING,
                    axis="allowed",
                    old_value=None,
                    new_value=path,
                )
            )

    for path in old_allowed:
        if path not in new_allowed:
            changes.append(
                GrantChange(
                    path=path,
                    direction=GrantDirection.NARROWING,
                    axis="allowed",
                    old_value=path,
                    new_value=None,
                )
            )

    old_denied = set(old.denied_paths)
    new_denied = set(new.denied_paths)

    for path in new_denied:
        if path not in old_denied:
            # Adding a deny is narrowing scope
            changes.append(
                GrantChange(
                    path=path,
                    direction=GrantDirection.NARROWING,
                    axis="denied",
                    old_value=None,
                    new_value=path,
                )
            )

    for path in old_denied:
        if path not in new_denied:
            # Removing a deny is widening scope
            changes.append(
                GrantChange(
                    path=path,
                    direction=GrantDirection.WIDENING,
                    axis="denied",
                    old_value=path,
                    new_value=None,
                )
            )

    def _sort_key(c: GrantChange) -> tuple[str, str, str]:
        return (c.axis, c.direction.value, c.path)

    changes.sort(key=_sort_key)

    return GrantDelta(
        role=role,
        run_id=run_id,
        changes=tuple(changes),
        timestamp_ns=timestamp_ns,
    )
