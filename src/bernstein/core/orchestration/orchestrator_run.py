"""Scheduled dependency-scan helpers for the orchestrator.

This module was opened by the ORCH-009 decomposition to hold the run loop,
with ``Orchestrator`` kept as the public facade. The extraction landed; the
delegation never did, so ``Orchestrator.run()`` kept its own copy and nothing
ever called the one here.

A dead duplicate would have been harmless if it had stayed identical. It did
not: the copy here hardcoded the failure ceiling the live loop reads from
config, never recorded ``RunClosureOutcome.FAILED``, and - after #4872 gave the
live loop a ``_pace`` seam - still paced with a bare ``time.sleep``. The
repository held both a bug and its fix, with only one of them reachable.
Removed in #4882; the run loop lives in ``Orchestrator.run()``, and there is
one of it.

What remains here is the scheduled dependency-scan path, which is live and is
reached through these module-level helpers.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _run_scheduled_dependency_scan(orch: Any) -> None:
    """Run the weekly dependency scan and enqueue remediation tasks.

    Args:
        orch: The orchestrator instance.
    """
    from bernstein.core.orchestration.dependency_scan_tasks import run_scheduled_dependency_scan

    run_scheduled_dependency_scan(orch)


def _load_existing_dependency_scan_task_titles(orch: Any) -> set[str]:
    """Load open remediation task titles so weekly scans do not duplicate them.

    Args:
        orch: The orchestrator instance.

    Returns:
        Set of existing task titles.
    """
    from bernstein.core.orchestration.dependency_scan_tasks import (
        load_existing_dependency_scan_task_titles,
    )

    return load_existing_dependency_scan_task_titles(orch)


def _create_dependency_fix_task(
    orch: Any,
    finding: Any,
    existing_titles: set[str],
) -> str | None:
    """Create one remediation task per vulnerable package.

    Args:
        orch: The orchestrator instance.
        finding: The vulnerability finding.
        existing_titles: Set of existing task titles for dedup.

    Returns:
        The title of the created task, or None if skipped/failed.
    """
    from bernstein.core.orchestration.dependency_scan_tasks import create_dependency_fix_task

    return create_dependency_fix_task(orch, finding, existing_titles)
