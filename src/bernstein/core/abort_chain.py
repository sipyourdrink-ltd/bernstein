"""Parent-to-child abort propagation for agent delegation trees.

When a parent agent is killed or aborted, all child agents spawned by it
must also receive an abort signal.  :class:`AbortChain` tracks the
parent-child relationship graph and cascades ``SHUTDOWN`` signals to
every descendant.

Usage::

    chain = AbortChain(signals_dir=workdir / ".sdd" / "runtime" / "signals")
    chain.register_child(parent_session_id, child_session_id)
    ...
    chain.propagate_abort(parent_session_id)  # cascades SHUTDOWN to all children
    chain.cleanup(parent_session_id)
"""

from __future__ import annotations

import logging
from collections import deque
from threading import Lock
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class AbortChainEntry(TypedDict):
    """Edge in the abort chain: parent session ID to a set of child session IDs."""

    children: set[str]


class AbortChain:
    """Tracks parent-child agent relationships and propagates abort signals.

    The abort chain is a DAG where each node is an agent session ID and edges
    represent "spawned by" relationships.  When ``propagate_abort`` is called
    on a parent, the ``SHUTDOWN`` signal file is written for every descendant
    in the subtree.

    Thread-safe: all mutations are guarded by a lock since the orchestrator
    tick runs in a single thread but tests may exercise concurrently.

    Args:
        signals_dir: Path to ``.sdd/runtime/signals/`` where ``SHUTDOWN``
            files are written.
    """

    __slots__ = ("_graph", "_lock", "_signals_dir")

    def __init__(self, *, signals_dir: Path) -> None:
        self._signals_dir = signals_dir
        self._graph: dict[str, set[str]] = {}
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

        logger.debug("AbortChain: registered child %s under parent %s", child_session_id, parent_session_id)

    # ------------------------------------------------------------------
    # Propagation
    # ------------------------------------------------------------------

    def propagate_abort(self, session_id: str) -> list[str]:
        """Cascade an abort signal to all descendants of *session_id*.

        Walks the subtree rooted at *session_id* in breadth-first order and
        writes a ``SHUTDOWN`` file for every child session found.
        Returns the list of session IDs that received the signal.

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
