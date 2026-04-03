"""Parent-to-child abort propagation for agent delegation trees.

When a parent agent is killed or aborted, all child agents spawned by it
must also receive an abort signal.  :class:`AbortChain` tracks the
parent-child relationship graph and cascades ``SHUTDOWN`` signals to
every descendant.

Three distinct abort scopes are supported:

* **TOOL** — :meth:`AbortChain.abort_tool` writes a ``TOOL_ABORT`` signal for
  a single tool invocation; the agent session itself is not stopped.
* **SIBLING** — :meth:`AbortChain.abort_siblings` sends ``SHUTDOWN`` to every
  peer agent spawned by the same parent, without touching the parent session.
* **SESSION** — :meth:`AbortChain.propagate_abort` cascades ``SHUTDOWN`` to all
  descendant sessions in the subtree (existing behaviour).

Scopes compose via :class:`AbortPolicy`::

    chain = AbortChain(signals_dir=workdir / ".sdd" / "runtime" / "signals")
    chain.register_child(parent_session_id, child_session_id)
    ...
    # Per-tool abort that also cascades to siblings:
    policy = AbortPolicy(tool_to_sibling=True, sibling_to_session=False)
    chain.abort_tool(child_session_id, "Bash", "exit 1", policy=policy)

    # Session-level abort (existing behaviour):
    chain.propagate_abort(parent_session_id)  # cascades SHUTDOWN to all children
    chain.cleanup(parent_session_id)
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abort scope and policy types
# ---------------------------------------------------------------------------


class AbortScope(StrEnum):
    """Granularity level for an abort operation.

    The hierarchy from most localised to most broad:
    ``TOOL`` → ``SIBLING`` → ``SESSION``.  Each level can be configured to
    propagate up or contain the failure at its own scope via
    :class:`AbortPolicy`.

    Attributes:
        TOOL: Abort only a single tool invocation; the agent session continues.
        SIBLING: Abort peer agents (same parent) without stopping the parent session.
        SESSION: Abort the full agent session and cascade SHUTDOWN to all descendants.
    """

    TOOL = "tool"
    SIBLING = "sibling"
    SESSION = "session"


@dataclass(frozen=True)
class AbortPolicy:
    """Configures how failures propagate through the three-level abort hierarchy.

    Operators use this to decide whether a failure at one scope level should
    automatically escalate to the next level, or be contained.

    Attributes:
        tool_to_sibling: If ``True``, a per-tool abort also aborts sibling agents.
        sibling_to_session: If ``True``, a sibling abort also tears down the
            parent session via :meth:`AbortChain.propagate_abort`.
    """

    tool_to_sibling: bool = False
    sibling_to_session: bool = False


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------


class AbortChainEntry(TypedDict):
    """Edge in the abort chain: parent session ID to a set of child session IDs."""

    children: set[str]


class ToolAbortRecord(TypedDict):
    """Record of a per-tool abort appended to ``TOOL_ABORT`` signal files.

    Attributes:
        session_id: The agent session that owns the tool invocation.
        tool: Name of the tool that was aborted (e.g. ``"Bash"``, ``"Edit"``).
        reason: Human-readable reason for the abort.
        ts: Unix timestamp of the abort.
    """

    session_id: str
    tool: str
    reason: str
    ts: float


# ---------------------------------------------------------------------------
# AbortChain
# ---------------------------------------------------------------------------


class AbortChain:
    """Tracks parent-child agent relationships and propagates abort signals.

    The abort chain is a DAG where each node is an agent session ID and edges
    represent "spawned by" relationships.  The chain supports three composable
    abort scopes (see :class:`AbortScope`):

    * **per-tool** — :meth:`abort_tool` — writes a ``TOOL_ABORT`` signal file
      in the session's signals directory.  The agent may continue after reading
      the file.  Optionally cascades to siblings via :class:`AbortPolicy`.
    * **sibling** — :meth:`abort_siblings` — sends ``SHUTDOWN`` to every peer
      of the triggering session (other children of the same parent) without
      touching the parent session.  Optionally cascades to the parent session.
    * **session** — :meth:`propagate_abort` — cascades ``SHUTDOWN`` to the
      full descendant subtree of the aborted session.

    Cleanup at each level is independent: a session-level abort cleans up the
    parent node; a sibling abort only removes individual child nodes.

    Thread-safe: all mutations are guarded by a lock since the orchestrator
    tick runs in a single thread but tests may exercise concurrently.

    Args:
        signals_dir: Path to ``.sdd/runtime/signals/`` where signal files are
            written (``SHUTDOWN``, ``TOOL_ABORT``).
    """

    __slots__ = ("_graph", "_lock", "_parent_of", "_signals_dir")

    def __init__(self, *, signals_dir: Path) -> None:
        self._signals_dir = signals_dir
        self._graph: dict[str, set[str]] = {}  # parent  → {children}
        self._parent_of: dict[str, str] = {}  # child   → parent  (reverse index)
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_child(self, parent_session_id: str, child_session_id: str) -> None:
        """Record that *child_session_id* was spawned by *parent_session_id*.

        Args:
            parent_session_id: The parent agent's session UUID.
            child_session_id: The child agent's session UUID.
        """
        with self._lock:
            if parent_session_id not in self._graph:
                self._graph[parent_session_id] = set()
            self._graph[parent_session_id].add(child_session_id)
            # Maintain reverse index for sibling-discovery.
            # If the child already has a different parent (diamond graph),
            # we keep the first registration — sibling abort uses this for
            # a single canonical parent lookup.
            self._parent_of.setdefault(child_session_id, parent_session_id)

        logger.debug("AbortChain: registered child %s under parent %s", child_session_id, parent_session_id)

    # ------------------------------------------------------------------
    # Per-tool abort (TOOL scope)
    # ------------------------------------------------------------------

    def abort_tool(
        self,
        session_id: str,
        tool_name: str,
        reason: str,
        *,
        policy: AbortPolicy | None = None,
    ) -> list[str]:
        """Write a per-tool abort record without stopping the agent session.

        Appends a JSON line to the ``TOOL_ABORT`` signal file in the session's
        signals directory.  The agent reads this file to detect tool-level
        aborts and can decide to retry or skip the failed tool invocation.

        If *policy.tool_to_sibling* is ``True``, also aborts sibling agents
        (see :meth:`abort_siblings`).

        Args:
            session_id: The agent session that owns the failing tool invocation.
            tool_name: Name of the tool being aborted (e.g. ``"Bash"``).
            reason: Human-readable abort reason.
            policy: Optional propagation policy.  Defaults to contain-only
                (no escalation to sibling or session level).

        Returns:
            List of additional session IDs that received a ``SHUTDOWN`` signal
            when the policy escalated the abort to sibling level.  Empty list
            when contained.
        """
        record: ToolAbortRecord = {
            "session_id": session_id,
            "tool": tool_name,
            "reason": reason,
            "ts": time.time(),
        }
        try:
            signal_dir = self._signals_dir / session_id
            signal_dir.mkdir(parents=True, exist_ok=True)
            tool_abort_file = signal_dir / "TOOL_ABORT"
            with tool_abort_file.open("a", encoding="utf-8") as fh:
                json.dump(record, fh, separators=(",", ":"))
                fh.write("\n")
            logger.info(
                "AbortChain: TOOL_ABORT written for session %s tool=%s reason=%r",
                session_id,
                tool_name,
                reason,
            )
        except OSError as exc:
            logger.warning("AbortChain: failed to write TOOL_ABORT for %s: %s", session_id, exc)

        # Optionally escalate to sibling level.
        cascaded: list[str] = []
        if policy is not None and policy.tool_to_sibling:
            cascaded = self.abort_siblings(
                session_id,
                triggering_session_id=session_id,
                reason=f"tool_abort:{tool_name}:{reason}",
                policy=policy,
            )

        return cascaded

    # ------------------------------------------------------------------
    # Sibling abort (SIBLING scope)
    # ------------------------------------------------------------------

    def abort_siblings(
        self,
        session_id: str,
        triggering_session_id: str,
        reason: str,
        *,
        policy: AbortPolicy | None = None,
    ) -> list[str]:
        """Send SHUTDOWN to every sibling of *session_id* (same parent, excluding self).

        Siblings are all children of the same parent as *session_id*.  The
        parent session itself is **not** stopped unless
        *policy.sibling_to_session* is ``True``, in which case
        :meth:`propagate_abort` is also called on the parent.

        Args:
            session_id: The session whose siblings should be aborted.
            triggering_session_id: The session that triggered the sibling abort
                (used in ``SHUTDOWN`` file content).
            reason: Human-readable reason for the sibling abort.
            policy: Optional propagation policy.

        Returns:
            List of session IDs that received a ``SHUTDOWN`` signal (siblings
            plus any descendant sessions if the policy escalated to session level).
        """
        with self._lock:
            parent_id = self._parent_of.get(session_id)

        if parent_id is None:
            logger.debug("AbortChain: sibling abort for %s — no parent found, nothing to abort", session_id)
            return []

        with self._lock:
            siblings = set(self._graph.get(parent_id, set())) - {session_id}

        aborted: list[str] = []
        for sibling_id in siblings:
            if self._write_sibling_shutdown(sibling_id, triggering_session_id, reason):
                aborted.append(sibling_id)

        if aborted:
            logger.info(
                "AbortChain: sibling abort from %s — sent SHUTDOWN to %d sibling(s): %s",
                triggering_session_id,
                len(aborted),
                ", ".join(aborted),
            )

        # Optionally escalate to session level.
        if policy is not None and policy.sibling_to_session:
            cascaded = self.propagate_abort(parent_id)
            aborted.extend(cascaded)
            self.cleanup(parent_id)

        return aborted

    # ------------------------------------------------------------------
    # Session-level abort (SESSION scope)
    # ------------------------------------------------------------------

    def propagate_abort(self, session_id: str) -> list[str]:
        """Cascade an abort signal to all descendants of *session_id*.

        Walks the subtree rooted at *session_id* in breadth-first order and
        writes a ``SHUTDOWN`` file for every child session found.
        Returns the list of session IDs that received the signal.

        This is the **SESSION** scope abort.  For more localised aborts use
        :meth:`abort_tool` (TOOL scope) or :meth:`abort_siblings` (SIBLING scope).

        Args:
            session_id: The session being aborted (its children receive the
                cascaded signal).

        Returns:
            List of child session IDs that were sent a ``SHUTDOWN`` signal.
        """
        descendants = self._collect_descendants(session_id)
        aborted: list[str] = []

        for child_id in descendants:
            if self._write_shutdown(child_id, session_id):
                aborted.append(child_id)

        if aborted:
            logger.info(
                "AbortChain: propagated abort from %s to %d child(ren): %s",
                session_id,
                len(aborted),
                ", ".join(aborted),
            )

        return aborted

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, session_id: str) -> None:
        """Remove *session_id* from the abort chain.

        Removes *session_id* as a parent key.  Children of this session are
        **not** removed from the graph — they may still be running and need
        independent cleanup.  If *session_id* appears as a child of another
        parent, that edge is also removed.

        Args:
            session_id: The session ID to remove.
        """
        with self._lock:
            # Remove as parent key (its children references stay)
            self._graph.pop(session_id, None)

            # Remove as child from any parent
            for children in self._graph.values():
                children.discard(session_id)

            # Remove from reverse index
            self._parent_of.pop(session_id, None)

        logger.debug("AbortChain: cleaned up session %s", session_id)

    # ------------------------------------------------------------------
    # Introspection (tests / diagnostics)
    # ------------------------------------------------------------------

    def get_children(self, session_id: str) -> set[str]:
        """Return the direct children of *session_id*.

        Args:
            session_id: The parent session ID.

        Returns:
            Set of direct child session IDs (empty if none).
        """
        with self._lock:
            return set(self._graph.get(session_id, set()))

    def get_parent(self, session_id: str) -> str | None:
        """Return the registered parent of *session_id*, if any.

        Args:
            session_id: The child session ID.

        Returns:
            Parent session ID, or ``None`` if not registered.
        """
        with self._lock:
            return self._parent_of.get(session_id)

    def get_siblings(self, session_id: str) -> set[str]:
        """Return sibling sessions of *session_id* (peers under the same parent).

        Args:
            session_id: The session whose siblings to find.

        Returns:
            Set of sibling session IDs (excludes *session_id* itself; empty if
            no parent or no other siblings).
        """
        with self._lock:
            parent_id = self._parent_of.get(session_id)
            if parent_id is None:
                return set()
            return set(self._graph.get(parent_id, set())) - {session_id}

    def size(self) -> int:
        """Return the total number of tracked edges."""
        with self._lock:
            return sum(len(children) for children in self._graph.values())

    def snapshot(self) -> dict[str, set[str]]:
        """Return a copy of the full graph for diagnostics.

        Returns:
            Dict mapping parent session IDs to sets of child session IDs.
        """
        with self._lock:
            return {parent: set(children) for parent, children in self._graph.items()}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_descendants(self, session_id: str) -> list[str]:
        """BFS collect all descendants of *session_id*.

        Args:
            session_id: Root of the subtree to walk.

        Returns:
            Ordered list of all descendant session IDs.
        """
        with self._lock:
            children = set(self._graph.get(session_id, set()))

        visited: set[str] = set()
        result: list[str] = []
        queue: deque[str] = deque(children)

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            result.append(current)
            # Enforce the lock around every graph access
            with self._lock:
                grandchildren = set(self._graph.get(current, set()))
            for gc in grandchildren:
                if gc not in visited:
                    queue.append(gc)

        return result

    def _write_shutdown(self, child_session_id: str, parent_session_id: str) -> bool:
        """Write a SHUTDOWN signal file for *child_session_id*.

        Args:
            child_session_id: The child session receiving the abort.
            parent_session_id: The parent that triggered the abort (used in
                the shutdown message).

        Returns:
            ``True`` if the signal file was written successfully.
        """
        try:
            signal_dir = self._signals_dir / child_session_id
            signal_dir.mkdir(parents=True, exist_ok=True)
            content = (
                f"# ABORT CHAIN — Parent killed/aborted\n"
                f"Parent session: {parent_session_id}\n"
                f"Your session: {child_session_id}\n"
                f"Your parent agent was killed or aborted.\n"
                f"Save your work and exit immediately.\n"
            )
            (signal_dir / "SHUTDOWN").write_text(content, encoding="utf-8")
            logger.info(
                "AbortChain: SHUTDOWN written for child %s (parent %s)",
                child_session_id,
                parent_session_id,
            )
            return True
        except OSError as exc:
            logger.warning(
                "AbortChain: failed to write SHUTDOWN for child %s: %s",
                child_session_id,
                exc,
            )
            return False

    def _write_sibling_shutdown(
        self,
        sibling_session_id: str,
        triggering_session_id: str,
        reason: str,
    ) -> bool:
        """Write a SHUTDOWN signal file for a sibling session.

        The content distinguishes a sibling abort from a parent-cascade abort
        so agents can log / handle them differently.

        Args:
            sibling_session_id: The sibling session receiving the abort.
            triggering_session_id: The session that triggered the sibling abort.
            reason: Human-readable reason.

        Returns:
            ``True`` if the signal file was written successfully.
        """
        try:
            signal_dir = self._signals_dir / sibling_session_id
            signal_dir.mkdir(parents=True, exist_ok=True)
            content = (
                f"# ABORT CHAIN — Sibling aborted\n"
                f"Triggering session: {triggering_session_id}\n"
                f"Your session: {sibling_session_id}\n"
                f"Reason: {reason}\n"
                f"A sibling agent failed.  Save your work and exit immediately.\n"
            )
            (signal_dir / "SHUTDOWN").write_text(content, encoding="utf-8")
            logger.info(
                "AbortChain: sibling SHUTDOWN written for %s (triggered by %s)",
                sibling_session_id,
                triggering_session_id,
            )
            return True
        except OSError as exc:
            logger.warning(
                "AbortChain: failed to write sibling SHUTDOWN for %s: %s",
                sibling_session_id,
                exc,
            )
            return False
