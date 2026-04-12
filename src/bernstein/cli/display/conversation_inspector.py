"""Road-033: Agent conversation inspector.

Provides an interactive inspector view for agent NDJSON conversation logs.
Builds on ``conversation_export.parse_ndjson_log`` to compute per-role
statistics, search messages, and produce Rich-formatted output for
post-mortem debugging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.conversation_export import parse_ndjson_log

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Role display colours (Rich markup names)
# ---------------------------------------------------------------------------

_ROLE_COLORS: dict[str, str] = {
    "system": "dim yellow",
    "user": "green",
    "assistant": "cyan",
    "tool_result": "magenta",
}

# ---------------------------------------------------------------------------
# InspectorView — frozen aggregate of a parsed conversation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectorView:
    """Aggregated view of a parsed agent conversation.

    Attributes:
        messages: List of message dicts with keys: role, content, tokens,
            tool_name.
        total_tokens: Estimated total token count across all messages.
        total_messages: Number of messages in the conversation.
        by_role: Message count grouped by role.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0
    total_messages: int = 0
    by_role: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4) if text else 0


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_inspector_view(log_path: Path) -> InspectorView | None:
    """Parse an NDJSON log and produce an ``InspectorView``.

    Args:
        log_path: Path to the agent NDJSON log file.

    Returns:
        An ``InspectorView`` with computed stats, or ``None`` when the file
        is missing or yields zero messages.
    """
    if not log_path.exists():
        return None

    parsed = parse_ndjson_log(log_path)
    if not parsed:
        return None

    messages: list[dict[str, Any]] = []
    by_role: dict[str, int] = {}
    total_tokens = 0

    for msg in parsed:
        tokens = _estimate_tokens(msg.content)
        total_tokens += tokens
        by_role[msg.role] = by_role.get(msg.role, 0) + 1
        messages.append(
            {
                "role": msg.role,
                "content": msg.content,
                "tokens": tokens,
                "tool_name": msg.tool_name,
            }
        )

    return InspectorView(
        messages=messages,
        total_tokens=total_tokens,
        total_messages=len(messages),
        by_role=by_role,
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# Regex to detect fenced code blocks for syntax highlighting.
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def _highlight_code_blocks(text: str) -> str:
    """Wrap fenced code blocks in Rich syntax markup."""

    def _replace(m: re.Match[str]) -> str:
        lang = m.group(1) or "text"
        code = m.group(2).rstrip("\n")
        return f"[bold]```{lang}[/bold]\n[dim]{code}[/dim]\n[bold]```[/bold]"

    return _CODE_BLOCK_RE.sub(_replace, text)


def format_message(msg: dict[str, Any], index: int, *, show_tokens: bool = True) -> str:
    """Format a single message for Rich console output.

    Args:
        msg: Message dict from ``InspectorView.messages``.
        index: Zero-based message index.
        show_tokens: Whether to include the token estimate.

    Returns:
        A Rich-markup string representing the message.
    """
    role: str = msg.get("role", "unknown")
    content: str = msg.get("content", "")
    tokens: int = msg.get("tokens", 0)
    tool_name: str | None = msg.get("tool_name")

    color = _ROLE_COLORS.get(role, "white")
    header_parts = [f"[{color}][{index}] {role.upper()}[/{color}]"]

    if tool_name:
        header_parts.append(f"[bold magenta]({tool_name})[/bold magenta]")
    if show_tokens:
        header_parts.append(f"[dim]~{tokens} tokens[/dim]")

    header = "  ".join(header_parts)

    body = _highlight_code_blocks(content)
    # Truncate very long messages for readability.
    max_len = 2000
    if len(body) > max_len:
        body = body[:max_len] + "\n[dim]... (truncated)[/dim]"

    return f"{header}\n{body}"


def format_inspector_output(
    view: InspectorView,
    search: str | None = None,
    role_filter: str | None = None,
) -> str:
    """Format full inspector output with optional search/filter.

    Args:
        view: The inspector view to render.
        search: Optional substring to highlight and filter by.
        role_filter: If set, only show messages matching this role.

    Returns:
        A Rich-markup string suitable for ``console.print()``.
    """
    lines: list[str] = []

    # Header summary.
    lines.append("[bold]Conversation Inspector[/bold]")
    lines.append(f"  Messages: {view.total_messages}  |  ~{view.total_tokens} tokens")
    role_summary = "  ".join(f"{r}: {c}" for r, c in sorted(view.by_role.items()))
    lines.append(f"  By role: {role_summary}")
    lines.append("")

    # Determine which indices to show.
    indices = search_messages(view, search) if search is not None else list(range(len(view.messages)))

    for idx in indices:
        msg = view.messages[idx]
        if role_filter and msg.get("role") != role_filter:
            continue
        lines.append(format_message(msg, idx))
        lines.append("")  # blank separator

    if not lines or lines == [""]:
        lines.append("[dim]No matching messages.[/dim]")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_messages(view: InspectorView, query: str) -> list[int]:
    """Return indices of messages whose content contains *query*.

    The search is case-insensitive.

    Args:
        view: Inspector view to search.
        query: Substring to look for.

    Returns:
        List of zero-based indices of matching messages.
    """
    lower_q = query.lower()
    return [i for i, msg in enumerate(view.messages) if lower_q in str(msg.get("content", "")).lower()]
