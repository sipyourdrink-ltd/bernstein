"""Actionable next-step suggestions for common Bernstein CLI errors.

Maps known error patterns (exception types, message substrings, exit codes)
to human-readable remediation instructions.  The ``suggest`` function is the
single entry point consumed by CLI command error handlers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class ErrorSuggestion:
    """A structured remediation hint for a known error condition.

    Attributes:
        pattern: Regex pattern matched against the error message string.
        title: Short one-line summary of the problem.
        steps: Ordered list of actionable remediation instructions.
    """

    pattern: str
    title: str
    steps: list[str]


# ---------------------------------------------------------------------------
# Top-20 common error -> suggestion mapping
# ---------------------------------------------------------------------------

_SUGGESTIONS: list[ErrorSuggestion] = [
    ErrorSuggestion(
        pattern=r"(?i)(?:port.*(?:in use|already)|EADDRINUSE|address already in use)",
        title="Port already in use",
        steps=[
            "Run 'bernstein stop' to shut down the existing server.",
            "Or start on a different port: bernstein --port 8053",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)cannot reach.*task server|connection refused|connect error",
        title="Task server unreachable",
        steps=[
            "Start Bernstein first: bernstein",
            "Check if the server is running: bernstein status",
            "Run diagnostics: bernstein doctor",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)ANTHROPIC_API_KEY.*not set|missing.*api.?key.*claude",
        title="Claude API key not configured",
        steps=[
            "export ANTHROPIC_API_KEY=sk-ant-...",
            "Or authenticate via OAuth: claude login",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)OPENAI_API_KEY.*not set|missing.*api.?key.*codex",
        title="OpenAI API key not configured",
        steps=[
            "export OPENAI_API_KEY=sk-...",
            "Or authenticate via login: codex login",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)GOOGLE_API_KEY.*not set|missing.*api.?key.*gemini",
        title="Google API key not configured",
        steps=[
            "export GOOGLE_API_KEY=...",
            "Or authenticate via gcloud: gcloud auth login",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)no.*cli.*agent.*found|no supported.*agent",
        title="No CLI agent installed",
        steps=[
            "Install at least one: claude, codex, or gemini CLI",
            "Verify installation: bernstein doctor",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)no.*seed.*file|no.*bernstein\.yaml|no goal.*seed",
        title="No project configuration found",
        steps=[
            "Create a bernstein.yaml: bernstein init",
            'Or pass an inline goal: bernstein -g "your goal here"',
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)budget.*exhaust|budget.*exceeded|spending.*cap.*reached",
        title="Budget limit reached",
        steps=[
            "Increase the budget: bernstein --budget 10.00",
            "Check current spend: bernstein cost",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)rate.?limit|429|too many requests|usage limit|quota exceeded",
        title="API rate limit hit",
        steps=[
            "Wait a few minutes for the rate limit to reset.",
            "Switch to a different model: bernstein --model sonnet",
            "Check API quota: bernstein doctor",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)permission denied|EACCES|not authorized|403",
        title="Permission denied",
        steps=[
            "Check file permissions in the project directory.",
            "Ensure your API key has the required scopes.",
            "Run 'bernstein doctor' to diagnose auth issues.",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)yaml.*(?:parse|syntax|scan)|invalid.*yaml",
        title="YAML syntax error in configuration",
        steps=[
            "Validate your YAML: bernstein validate bernstein.yaml",
            "Check for indentation errors and missing colons.",
            "See 'bernstein help-all' for configuration format.",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)\.sdd.*(?:missing|not found|incomplete)|workspace.*missing",
        title="Workspace not initialized",
        steps=[
            "Initialize the workspace: bernstein init",
            'Or start a run which auto-initializes: bernstein -g "goal"',
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)timeout|timed? ?out|deadline exceeded",
        title="Operation timed out",
        steps=[
            "Check network connectivity to the API provider.",
            "The task server may be overloaded -- check 'bernstein status'.",
            "Try again with a longer timeout.",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)disk.*(?:full|space)|no space left|ENOSPC",
        title="Disk space exhausted",
        steps=[
            "Free disk space: bernstein cleanup",
            "Remove old metrics: rm -rf .sdd/metrics/api_usage_*.jsonl",
            "Check disk usage: du -sh .sdd/",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)git.*(?:conflict|merge|rebase)|CONFLICT",
        title="Git merge conflict",
        steps=[
            "Resolve conflicts manually, then: bernstein",
            "Or reset the branch: git checkout -- .",
            "Review pending changes: bernstein diff",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)bootstrap.*fail|startup.*fail",
        title="Server startup failed",
        steps=[
            "Check logs: cat .sdd/runtime/server.log",
            "Run diagnostics: bernstein doctor --fix",
            "Try a clean restart: bernstein stop && bernstein",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)adapter.*(?:not found|unavailable|unsupported)",
        title="Adapter not available",
        steps=[
            "List available adapters: bernstein agents list",
            "Install the adapter CLI tool and retry.",
            "Force a specific adapter: bernstein --cli claude",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)stale.*pid|orphan.*process|zombie",
        title="Stale processes detected",
        steps=[
            "Clean up: bernstein stop --force",
            "Run auto-fix: bernstein doctor --fix",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)import.*error|module.*not found|no module named",
        title="Missing Python dependency",
        steps=[
            "Reinstall: pip install -e .",
            "Or with extras: pip install bernstein[all]",
        ],
    ),
    ErrorSuggestion(
        pattern=r"(?i)json.*(?:decode|parse)|invalid.*json|expecting value",
        title="Corrupt JSON data",
        steps=[
            "The state file may be corrupt. Try: bernstein cleanup",
            "Or reset state: rm -rf .sdd/runtime/ && bernstein",
        ],
    ),
]

# Compiled patterns for faster matching
_COMPILED: list[tuple[re.Pattern[str], ErrorSuggestion]] = [(re.compile(s.pattern), s) for s in _SUGGESTIONS]


def suggest(error: str | BaseException) -> ErrorSuggestion | None:
    """Match an error to the best remediation suggestion.

    Args:
        error: An exception instance or error message string.

    Returns:
        The first matching ``ErrorSuggestion``, or ``None`` if no known
        pattern matches.
    """
    message = str(error)
    for compiled_pattern, suggestion in _COMPILED:
        if compiled_pattern.search(message):
            return suggestion
    return None


def format_suggestion(suggestion: ErrorSuggestion) -> str:
    """Format a suggestion into a human-readable multi-line string.

    Args:
        suggestion: The suggestion to format.

    Returns:
        Formatted string with title and numbered steps.
    """
    lines = [f"  Suggestion: {suggestion.title}"]
    for i, step in enumerate(suggestion.steps, 1):
        lines.append(f"    {i}. {step}")
    return "\n".join(lines)


def suggest_and_format(error: str | BaseException) -> str:
    """Convenience: match + format in one call.

    Args:
        error: An exception instance or error message string.

    Returns:
        Formatted suggestion string, or empty string if no match.
    """
    s = suggest(error)
    if s is None:
        return ""
    return format_suggestion(s)


def all_suggestions() -> Sequence[ErrorSuggestion]:
    """Return all registered error suggestions (read-only).

    Returns:
        Sequence of all ``ErrorSuggestion`` objects.
    """
    return list(_SUGGESTIONS)
