"""Prompt-builder helpers for ``bernstein resume``.

Adapters that *do not* implement the optional :py:meth:`CLIAdapter.resume`
capability fall back to spawning a fresh session. To preserve continuity
they need the prior scratchpad re-injected into the agent prompt as
recovered context. This module produces that injection block from a
:class:`TaskResumeCheckpoint`.

Kept separate from :mod:`bernstein.core.persistence.task_resume` so the
checkpoint storage layer stays free of agent-prompt concerns.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.persistence.task_resume import TaskResumeCheckpoint

__all__ = [
    "RESUME_BANNER",
    "build_resume_context",
    "read_scratchpad",
]


RESUME_BANNER: str = (
    "## Resume context\n"
    "You are resuming a previously interrupted task. Use the captured "
    "scratchpad below as recovered context and continue from the next "
    "step boundary - do not restart from scratch.\n"
)
"""Top-of-prompt banner injected when an adapter falls back to fresh."""


def read_scratchpad(scratchpad_path: str | Path | None) -> str:
    """Return the scratchpad file's text, or empty string when missing.

    Robust to ``None`` paths, missing files, and decode errors so a
    failed read never blocks a resume.
    """
    if scratchpad_path is None:
        return ""
    path = Path(scratchpad_path)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def build_resume_context(checkpoint: TaskResumeCheckpoint) -> str:
    """Build the resume-context prompt block from ``checkpoint``.

    The block is safe to prepend verbatim to any adapter prompt. It always
    starts with :data:`RESUME_BANNER` and includes:

    * The last completed step id (so the agent knows where to resume).
    * Resume attempt number (so the agent can adapt strategy if flaky).
    * The fenced scratchpad contents (or a placeholder when empty).
    """
    parts: list[str] = [RESUME_BANNER]
    parts.extend(
        (
            f"- last_completed_step_id: {checkpoint.last_completed_step_id or '<none>'}",
            f"- resume_attempt: {checkpoint.resume_count}",
        )
    )
    if checkpoint.adapter_session_id:
        parts.append(f"- prior_adapter_session_id: {checkpoint.adapter_session_id}")
    scratchpad_text = read_scratchpad(checkpoint.scratchpad_path)
    parts.extend(("", "### Recovered scratchpad"))
    if scratchpad_text.strip():
        parts.extend(("```", scratchpad_text.rstrip(), "```"))
    else:
        parts.append("_(empty - no scratchpad was captured before interruption)_")
    parts.append("")
    return "\n".join(parts)
