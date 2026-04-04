"""Periodic activity summary broadcaster for background agents.

Runs a daemon thread that every *interval* seconds reads the current
activity state from an :class:`~bernstein.activity_tracker.ActivitySession`
and posts an :class:`~bernstein.core.bulletin.AgentActivitySummary` to a
:class:`~bernstein.core.bulletin.BulletinBoard`.

This gives the dashboard and other agents a continuously-fresh 3-5 word
description of what each background agent is currently doing.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.activity_tracker import ActivitySession
    from bernstein.core.bulletin import BulletinBoard

from bernstein.core.bulletin import AgentActivitySummary

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 30.0


@dataclass
class ActivitySummaryPoller:
    """Publish 3-5 word activity summaries to the bulletin board every *interval* seconds.

    Spawns a single daemon thread on :meth:`start`.  The thread calls
    :meth:`poll_once` on each wake-up, then sleeps for *interval* seconds.
    Calling :meth:`stop` signals the thread to exit gracefully.

    When *sdd_dir* is provided, each poll also writes the summary to
    ``{sdd_dir}/runtime/activity_summaries/{agent_id}.json`` so the
    dashboard process can read it without HTTP overhead.

    Attributes:
        agent_id: Identifier of the agent whose activity is tracked.
        session: The :class:`~bernstein.activity_tracker.ActivitySession` to
            query for the current summary text.
        board: The :class:`~bernstein.core.bulletin.BulletinBoard` to post
            summaries to.
        interval: Seconds between successive polls (default: 30.0).
        sdd_dir: Optional path to the ``.sdd`` directory.  When set, summaries
            are also written to disk for cross-process dashboard access.
    """

    agent_id: str
    session: ActivitySession
    board: BulletinBoard
    interval: float = _DEFAULT_INTERVAL
    sdd_dir: Path | None = None

    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background polling thread.

        No-op if the thread is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"activity-summary-poller-{self.agent_id}",
            daemon=True,
        )
        self._thread.start()
        logger.debug("ActivitySummaryPoller started for agent %s (interval=%.1fs)", self.agent_id, self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the polling thread to stop and wait for it to exit.

        Args:
            timeout: Maximum seconds to wait for the thread to finish.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.debug("ActivitySummaryPoller stopped for agent %s", self.agent_id)

    def poll_once(self) -> AgentActivitySummary:
        """Generate and post one activity summary now.

        Reads the current summary text from the session, wraps it in an
        :class:`~bernstein.core.bulletin.AgentActivitySummary`, posts it to
        the board, and returns the posted object.  If *sdd_dir* is set the
        summary is also written to
        ``{sdd_dir}/runtime/activity_summaries/{agent_id}.json`` for
        cross-process dashboard access.

        Returns:
            The :class:`~bernstein.core.bulletin.AgentActivitySummary` that
            was posted.
        """
        summary_text = self.session.get_summary()
        activity_summary = AgentActivitySummary(
            agent_id=self.agent_id,
            summary=summary_text,
        )
        self.board.post_activity_summary(activity_summary)
        if self.sdd_dir is not None:
            self._write_to_disk(activity_summary)
        logger.debug("ActivitySummaryPoller posted '%s' for agent %s", summary_text, self.agent_id)
        return activity_summary

    def _write_to_disk(self, activity_summary: AgentActivitySummary) -> None:
        """Write *activity_summary* to the cross-process summary file.

        Writes atomically (write to temp file, then replace) to avoid partial
        reads by the dashboard.

        Args:
            activity_summary: The summary to persist.
        """
        if self.sdd_dir is None:
            return
        out_dir = self.sdd_dir / "runtime" / "activity_summaries"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    "agent_id": activity_summary.agent_id,
                    "summary": activity_summary.summary,
                    "timestamp": activity_summary.timestamp,
                }
            )
            dest = out_dir / f"{self.agent_id}.json"
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(dest)
        except Exception:
            logger.warning("ActivitySummaryPoller failed to write summary to disk for %s", self.agent_id, exc_info=True)

    @property
    def is_running(self) -> bool:
        """Return True if the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Thread entry point: poll immediately, then on each interval."""
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.warning("ActivitySummaryPoller poll_once failed for agent %s", self.agent_id, exc_info=True)
            # Sleep in small increments so stop() can interrupt promptly.
            self._stop_event.wait(timeout=self.interval)
