"""Task completion / post-completion processing.

This module holds helpers for parsing agent logs into a completion payload.
It no longer contains retry / escalation logic — that lives exclusively in
:mod:`bernstein.core.tasks.task_lifecycle` (see audit-017).  The previous
stale copies of ``maybe_retry_task`` / ``retry_or_fail_task`` read a
``[RETRY N]`` title prefix / ``[retry:N]`` description marker and were
removed because they disagreed with the typed ``Task.retry_count`` field
and caused retry-counter drift / runaway retries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.agent_log_aggregator import AgentLogAggregator

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tasks.models import AgentSession
    from bernstein.core.tick_pipeline import CompletionData


# ---------------------------------------------------------------------------
# Completion data extraction
# ---------------------------------------------------------------------------


def collect_completion_data(workdir: Path, session: AgentSession) -> CompletionData:
    """Read agent log file and extract structured completion data.

    Parses the agent's runtime log into a backward-compatible completion payload.

    Args:
        workdir: Project working directory.
        session: Agent session whose log to parse.

    Returns:
        Dict with files_modified, test_results, and optional log_summary keys.
    """
    aggregator = AgentLogAggregator(workdir)
    summary = aggregator.parse_log(session.id)
    data: CompletionData = {
        "files_modified": list(summary.files_modified),
        "test_results": {},
    }
    if aggregator.log_exists(session.id) and summary.total_lines > 0:
        data["log_summary"] = summary
    if summary.test_summary:
        data["test_results"] = {"summary": summary.test_summary}
    return data
