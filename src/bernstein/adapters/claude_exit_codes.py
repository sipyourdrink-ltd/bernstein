"""Map Claude Code exit codes to Bernstein AbortReason/TransitionReason.

Claude Code uses specific exit codes to signal different termination
conditions.  This module maps those codes to Bernstein's lifecycle
enums so the orchestrator can make informed retry/abort decisions.

Exit code semantics (from Claude Code CLI docs):
- 0: Success — task completed normally
- 1: General error — unspecified failure
- 2: User interrupt (SIGINT / Ctrl+C)
- 3: Context window overflow — conversation exceeded model context
- 4: Permission denied — agent attempted disallowed action
- 130: SIGINT (128 + 2) — process killed by interrupt signal
- 137: SIGKILL (128 + 9) — process killed forcefully (OOM or timeout)
- 143: SIGTERM (128 + 15) — graceful shutdown requested
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bernstein.core.models import AbortReason, TransitionReason

_TASK_COMPLETED_MSG = "Task completed successfully"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExitInterpretation:
    """Interpretation of a Claude Code exit code.

    Attributes:
        exit_code: The raw exit code from the process.
        transition_reason: Bernstein TransitionReason for lifecycle state machine.
        abort_reason: Bernstein AbortReason, or None if not an abort.
        should_retry: Whether the orchestrator should retry this task.
        human_readable: Short description for logs and dashboards.
    """

    exit_code: int
    transition_reason: TransitionReason
    abort_reason: AbortReason | None
    should_retry: bool
    human_readable: str


# Mapping table: exit_code -> (transition, abort, should_retry, description)
_EXIT_CODE_MAP: dict[int, tuple[TransitionReason, AbortReason | None, bool, str]] = {
    0: (
        TransitionReason.COMPLETED,
        None,
        False,
        _TASK_COMPLETED_MSG,
    ),
    1: (
        TransitionReason.ABORTED,
        AbortReason.PROVIDER_ERROR,
        True,
        "General error (unspecified failure)",
    ),
    2: (
        TransitionReason.ABORTED,
        AbortReason.USER_INTERRUPT,
        False,
        "User interrupt (SIGINT)",
    ),
    3: (
        TransitionReason.PROMPT_TOO_LONG,
        AbortReason.COMPACT_FAILURE,
        True,
        "Context window overflow",
    ),
    4: (
        TransitionReason.PERMISSION_DENIED,
        AbortReason.PERMISSION_DENIED,
        False,
        "Permission denied by Claude Code",
    ),
    130: (
        TransitionReason.ABORTED,
        AbortReason.USER_INTERRUPT,
        False,
        "Killed by SIGINT (130 = 128+2)",
    ),
    137: (
        TransitionReason.ABORTED,
        AbortReason.OOM,
        True,
        "Killed by SIGKILL (137 = 128+9, likely OOM or timeout)",
    ),
    143: (
        TransitionReason.ABORTED,
        AbortReason.SHUTDOWN_SIGNAL,
        True,
        "Killed by SIGTERM (143 = 128+15, graceful shutdown)",
    ),
}


def interpret_exit_code(exit_code: int) -> ExitInterpretation:
    """Map a Claude Code exit code to Bernstein lifecycle enums.

    Args:
        exit_code: Process exit code from Claude Code CLI.

    Returns:
        ExitInterpretation with transition reason, abort reason,
        retry recommendation, and human-readable description.
    """
    mapped = _EXIT_CODE_MAP.get(exit_code)
    if mapped is not None:
        transition, abort, retry, desc = mapped
        return ExitInterpretation(
            exit_code=exit_code,
            transition_reason=transition,
            abort_reason=abort,
            should_retry=retry,
            human_readable=desc,
        )

    # Handle signal-based exit codes (128 + signal_number)
    if exit_code > 128:
        signal_num = exit_code - 128
        return ExitInterpretation(
            exit_code=exit_code,
            transition_reason=TransitionReason.ABORTED,
            abort_reason=AbortReason.UNKNOWN,
            should_retry=True,
            human_readable=f"Killed by signal {signal_num} (exit code {exit_code})",
        )

    # Unknown non-zero exit code
    if exit_code != 0:
        return ExitInterpretation(
            exit_code=exit_code,
            transition_reason=TransitionReason.ABORTED,
            abort_reason=AbortReason.UNKNOWN,
            should_retry=True,
            human_readable=f"Unknown error (exit code {exit_code})",
        )

    # exit_code == 0 but not in the map (defensive fallback)
    return ExitInterpretation(
        exit_code=0,
        transition_reason=TransitionReason.COMPLETED,
        abort_reason=None,
        should_retry=False,
        human_readable=_TASK_COMPLETED_MSG,
    )


def interpret_result_subtype(subtype: str) -> ExitInterpretation:
    """Map a Claude Code result subtype to Bernstein lifecycle enums.

    Claude Code's stream-json result events include a ``subtype`` field
    that provides more context than the exit code alone.

    Args:
        subtype: Result subtype from the stream-json result event.
            Known values: "success", "error_max_turns", "error_model",
            "error_context_window", "error_permission".

    Returns:
        ExitInterpretation based on the subtype.
    """
    subtype_map: dict[str, tuple[TransitionReason, AbortReason | None, bool, str]] = {
        "success": (
            TransitionReason.COMPLETED,
            None,
            False,
            _TASK_COMPLETED_MSG,
        ),
        "error_max_turns": (
            TransitionReason.MAX_TURNS,
            None,
            True,
            "Agent exhausted maximum turns",
        ),
        "error_model": (
            TransitionReason.ABORTED,
            AbortReason.PROVIDER_ERROR,
            True,
            "Model/provider error during execution",
        ),
        "error_context_window": (
            TransitionReason.PROMPT_TOO_LONG,
            AbortReason.COMPACT_FAILURE,
            True,
            "Context window overflow during execution",
        ),
        "error_permission": (
            TransitionReason.PERMISSION_DENIED,
            AbortReason.PERMISSION_DENIED,
            False,
            "Permission denied during execution",
        ),
    }

    mapped = subtype_map.get(subtype)
    if mapped is not None:
        transition, abort, retry, desc = mapped
        return ExitInterpretation(
            exit_code=-1,  # Not from exit code; from stream result
            transition_reason=transition,
            abort_reason=abort,
            should_retry=retry,
            human_readable=desc,
        )

    # Unknown subtype
    logger.warning("Unknown Claude Code result subtype: %r", subtype)
    return ExitInterpretation(
        exit_code=-1,
        transition_reason=TransitionReason.ABORTED,
        abort_reason=AbortReason.UNKNOWN,
        should_retry=True,
        human_readable=f"Unknown result subtype: {subtype}",
    )
