"""CLAUDE-011: Cost tracking via Claude Code usage API (parse session cost output).

Parses Claude Code CLI session output to extract cost and token usage
data.  Handles multiple output formats including stream-json events,
session summary lines, and the structured result JSON.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger(__name__)

# Pattern for session cost summary lines in Claude Code output.
# Example: "Total cost: $0.42" or "Session cost: $1.23"
_COST_LINE_PATTERN = re.compile(
    r"(?:Total|Session)\s+cost:\s*\$?([\d.]+)",
    re.IGNORECASE,
)

# Pattern for token count lines.
# Example: "Input tokens: 12345, Output tokens: 6789"
_TOKEN_LINE_PATTERN = re.compile(
    r"(?:Input|Prompt)\s+tokens?:\s*(\d[\d,]*)"
    r".*?(?:Output|Completion)\s+tokens?:\s*(\d[\d,]*)",
    re.IGNORECASE,
)

# Pattern for usage in stream-json result events.
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass
class SessionCostData:
    """Parsed cost and usage data from a Claude Code session.

    Attributes:
        session_id: Agent session identifier.
        model: Model name used.
        input_tokens: Total input/prompt tokens.
        output_tokens: Total output/completion tokens.
        cache_read_tokens: Tokens served from prompt cache.
        cache_write_tokens: Tokens written to prompt cache.
        total_cost_usd: Total session cost in USD.
        duration_s: Session duration in seconds.
        turns: Number of conversation turns.
        parsed_at: Timestamp when this data was parsed.
    """

    session_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_s: float = 0.0
    turns: int = 0
    parsed_at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        """Total token count across all categories."""
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "session_id": self.session_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "duration_s": round(self.duration_s, 1),
            "turns": self.turns,
            "parsed_at": self.parsed_at,
        }


def _parse_int(s: str) -> int:
    """Parse an integer, stripping commas.

    Args:
        s: String that may contain commas (e.g. "12,345").

    Returns:
        Parsed integer.
    """
    return int(s.replace(",", ""))


def parse_session_output(
    output: str,
    *,
    session_id: str = "",
    model: str = "",
) -> SessionCostData:
    """Parse Claude Code session output for cost and usage data.

    Handles multiple output formats:
    1. Stream-json result events with usage dict
    2. Text-format summary lines ("Total cost: $X.XX")
    3. JSON result objects

    Args:
        output: Full output text from a Claude Code session.
        session_id: Agent session ID (for tracking).
        model: Model name if known.

    Returns:
        SessionCostData with parsed values.
    """
    data = SessionCostData(session_id=session_id, model=model)
    turns = 0

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Try JSON parsing first.
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                typed_obj = cast("dict[str, Any]", obj)
                _extract_from_json(typed_obj, data)
                # Count assistant turns.
                if typed_obj.get("type") == "assistant" or typed_obj.get("role") == "assistant":
                    turns += 1
                continue
        except json.JSONDecodeError:
            pass

        # Try regex patterns for text output.
        cost_match = _COST_LINE_PATTERN.search(stripped)
        if cost_match:
            data.total_cost_usd = max(data.total_cost_usd, float(cost_match.group(1)))

        token_match = _TOKEN_LINE_PATTERN.search(stripped)
        if token_match:
            data.input_tokens = max(data.input_tokens, _parse_int(token_match.group(1)))
            data.output_tokens = max(data.output_tokens, _parse_int(token_match.group(2)))

    data.turns = turns
    return data


def _extract_from_json(obj: dict[str, Any], data: SessionCostData) -> None:
    """Extract cost/usage data from a parsed JSON object.

    Mutates the SessionCostData in place with the highest values found.

    Args:
        obj: Parsed JSON dict.
        data: SessionCostData to update.
    """
    # Extract from usage dict.
    usage_raw: object = obj.get("usage", {})
    if isinstance(usage_raw, dict):
        usage = cast("dict[str, object]", usage_raw)
        data.input_tokens = max(data.input_tokens, int(str(usage.get("input_tokens", 0))))
        data.output_tokens = max(data.output_tokens, int(str(usage.get("output_tokens", 0))))
        cr_raw = str(usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0)))
        data.cache_read_tokens = max(data.cache_read_tokens, int(cr_raw))
        cw_raw = str(usage.get("cache_creation_input_tokens", usage.get("cache_write_tokens", 0)))
        data.cache_write_tokens = max(data.cache_write_tokens, int(cw_raw))

    # Extract cost.
    for cost_key in ("cost_usd", "cost", "total_cost", "session_cost"):
        cost_val: object = obj.get(cost_key)
        if cost_val is not None:
            with contextlib.suppress(ValueError, TypeError):
                data.total_cost_usd = max(data.total_cost_usd, float(cast("str | int | float", cost_val)))

    # Extract model.
    if not data.model:
        model_val: object = obj.get("model", "")
        if model_val:
            data.model = str(model_val)

    # Extract duration.
    for dur_key in ("duration_ms", "duration_s", "elapsed_ms"):
        dur_val: object = obj.get(dur_key)
        if dur_val is not None:
            with contextlib.suppress(ValueError, TypeError):
                dur = float(cast("str | int | float", dur_val))
                if "ms" in dur_key:
                    dur /= 1000.0
                data.duration_s = max(data.duration_s, dur)


@dataclass
class CostTrackingAggregator:
    """Aggregates cost data across multiple Claude Code sessions.

    Attributes:
        sessions: Per-session cost data.
    """

    sessions: dict[str, SessionCostData] = field(default_factory=dict[str, SessionCostData])

    def record_session(self, data: SessionCostData) -> None:
        """Record or update cost data for a session.

        Args:
            data: Parsed session cost data.
        """
        self.sessions[data.session_id] = data

    def total_cost_usd(self) -> float:
        """Total cost across all sessions.

        Returns:
            Sum of all session costs.
        """
        return sum(s.total_cost_usd for s in self.sessions.values())

    def total_tokens(self) -> int:
        """Total tokens across all sessions.

        Returns:
            Sum of all session token counts.
        """
        return sum(s.total_tokens for s in self.sessions.values())

    def summary(self) -> dict[str, Any]:
        """Aggregate summary across all tracked sessions.

        Returns:
            Dict with total cost, tokens, and per-model breakdown.
        """
        by_model: dict[str, dict[str, Any]] = {}
        for s in self.sessions.values():
            model = s.model or "unknown"
            if model not in by_model:
                by_model[model] = {"cost_usd": 0.0, "tokens": 0, "sessions": 0}
            by_model[model]["cost_usd"] += s.total_cost_usd
            by_model[model]["tokens"] += s.total_tokens
            by_model[model]["sessions"] += 1

        return {
            "total_sessions": len(self.sessions),
            "total_cost_usd": round(self.total_cost_usd(), 6),
            "total_tokens": self.total_tokens(),
            "by_model": {
                m: {k: round(v, 6) if isinstance(v, float) else v for k, v in d.items()} for m, d in by_model.items()
            },
        }
