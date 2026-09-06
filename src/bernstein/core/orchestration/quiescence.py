"""Whether every process a run started had actually exited when it was sealed.

Finalization seals the journal head into the lineage spine and writes the run
receipt. Nothing recorded whether execution had stopped first, so a tool
process that outlived its wrapper could still write into a worktree or the
integration branch *after* the receipt covering the run was produced, and the
record was silent about it (#5272).

This module answers the question and produces the record. It does not stop
anything: escalating with ``kill_process_group_graceful`` for what remains
after the drain timeout is the other half of that issue, and a report that is
honest about residual work is useful before anything acts on it.

The distinction the record has to keep is between *checked and clean* and
*could not check*. On a platform without process groups the answer is
``verified=False`` with ``method="unsupported"`` — never a silent true, because
"we did not look" and "nothing was there" are different claims and only one of
them is evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.config.platform_compat import IS_WINDOWS, process_group_alive

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "METHOD_PROCESS_GROUP",
    "METHOD_UNSUPPORTED",
    "QuiescenceReport",
    "ResidualGroup",
    "check_quiescence",
]

#: Groups were probed with the platform's process-group primitive.
METHOD_PROCESS_GROUP = "process_group"

#: The platform has no process groups, so nothing was probed.
METHOD_UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ResidualGroup:
    """One session whose process group still had members at the seal."""

    session_id: str
    pgid: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {"session_id": self.session_id, "pgid": self.pgid}


@dataclass(frozen=True, slots=True)
class QuiescenceReport:
    """What was checked, and what was still running.

    ``verified`` is True only when every group was probed and none had
    members. It is False both when something survived and when nothing could
    be probed; ``method`` and ``residual`` say which, so a reader is never
    left to infer it from an empty list.
    """

    verified: bool
    residual: tuple[ResidualGroup, ...]
    method: str
    checked: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization for the journal row."""
        return {
            "verified": self.verified,
            "residual": [group.to_dict() for group in self.residual],
            "method": self.method,
            "checked": self.checked,
        }


def check_quiescence(session_pgids: Mapping[str, int | None]) -> QuiescenceReport:
    """Report whether every session's process group has exited.

    Args:
        session_pgids: Session id to the pgid the session was started in.
            Under ``start_new_session=True`` that is the wrapper's pid. A
            ``None`` or non-positive value is a session with nothing to probe
            and is skipped rather than counted as clean.

    Returns:
        The report. On a platform without process groups, ``verified`` is
        False and ``method`` is :data:`METHOD_UNSUPPORTED`, whatever the probe
        would have said.
    """
    if IS_WINDOWS:
        # `process_group_alive` falls back to the lead pid here, which answers
        # a narrower question than this record claims to answer. Reporting
        # `verified=True` off that fallback would be the silent true the issue
        # rules out.
        return QuiescenceReport(
            verified=False,
            residual=(),
            method=METHOD_UNSUPPORTED,
            checked=0,
        )

    residual: list[ResidualGroup] = []
    checked = 0
    for session_id, pgid in sorted(session_pgids.items()):
        if pgid is None or pgid <= 0:
            continue
        checked += 1
        if process_group_alive(pgid):
            residual.append(ResidualGroup(session_id=session_id, pgid=pgid))
    return QuiescenceReport(
        verified=not residual,
        residual=tuple(residual),
        method=METHOD_PROCESS_GROUP,
        checked=checked,
    )
