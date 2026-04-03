"""Post-tool enforcement for audit and redaction.

After a tool executes, this module:
1. Inspects tool output for sensitive data patterns (secrets, PII).
2. Redacts detected patterns before persistence/display.
3. Writes structured audit records.
4. Optionally blocks continuation when dangerous patterns are found.

Inspired by T465 — mirrors pre-tool ``check_secrets`` on tool output.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction patterns — mirrors secret patterns from guardrails, applied to
# tool *output* (what the agent sees or what gets persisted).
# ---------------------------------------------------------------------------

_OUTPUT_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "generic_secret",
        re.compile(
            r"(?i)(?<![a-z_])(?:password|secret|api_key|apikey|access_token|auth_token)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-./+=]{8,}"
        ),
    ),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b")),
]

_REDACTED = "[REDACTED]"

# Dangerous output patterns that should BLOCK continuation.
_DANGEROUS_OUTPUT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Shell history containing secret exfiltration
    (
        "potential_data_exfil",
        re.compile(r"(?i)(?:curl|wget|scp|rsync).*(?:pastebin|transfer\.sh|ngrok|webhook)"),
    ),
]


@dataclass(frozen=True)
class AuditRecord:
    """Structured audit record for a tool execution outcome.

    Attributes:
        session_id: Agent session that ran the tool.
        tool: Tool name.
        tool_input: Tool arguments (redacted).
        raw_output_length: Length of the raw tool output.
        redacted_output_length: Length after redaction.
        secrets_found: List of secret pattern names detected.
        dangerous: Whether dangerous patterns were detected.
        blocked: Whether continuation should be blocked.
        timestamp: ISO-8601 timestamp of the audit.
    """

    session_id: str
    tool: str
    tool_input: dict[str, Any]
    raw_output_length: int
    redacted_output_length: int
    secrets_found: list[str]
    dangerous: bool
    blocked: bool
    timestamp: str


@dataclass(frozen=True)
class EnforcementResult:
    """Result of post-tool enforcement.

    Attributes:
        redacted_output: Tool output with secrets redacted.
        audit: Audit record for this enforcement run.
        should_block: True if dangerous patterns detected and continuation
            should be blocked.
    """

    redacted_output: str
    audit: AuditRecord
    should_block: bool = False


# ---------------------------------------------------------------------------
# Core enforcement
# ---------------------------------------------------------------------------


def run_post_tool_enforcement(
    session_id: str,
    tool: str,
    tool_input: dict[str, Any],
    raw_output: str,
    *,
    workdir: Path | None = None,
) -> EnforcementResult:
    """Inspect tool output, redact secrets, and produce an audit record.

    Args:
        session_id: Agent session identifier.
        tool: Name of the tool that was executed.
        tool_input: Tool input (used for the audit record).
        raw_output: Captured stdout/stderr from the tool.
        workdir: Project root; if provided, audit records are appended
            to ``.sdd/metrics/tool_audit.jsonl``.

    Returns:
        An :class:`EnforcementResult` with the redacted output, audit record,
        and a ``should_block`` flag.
    """
    secrets_found: list[str] = []
    redacted = raw_output

    for name, pattern in _OUTPUT_SECRET_PATTERNS:
        if pattern.search(redacted):
            secrets_found.append(name)
            redacted = pattern.sub(_REDACTED, redacted)

    dangerous = False
    for name, pattern in _DANGEROUS_OUTPUT_PATTERNS:
        if pattern.search(raw_output):
            dangerous = True
            logger.warning(
                "Dangerous pattern '%s' detected in tool output: session=%s, tool=%s",
                name,
                session_id,
                tool,
            )

    audit = AuditRecord(
        session_id=session_id,
        tool=tool,
        tool_input=tool_input,
        raw_output_length=len(raw_output),
        redacted_output_length=len(redacted),
        secrets_found=secrets_found,
        dangerous=dangerous,
        blocked=dangerous,
        timestamp=datetime.now(UTC).isoformat(),
    )

    if secrets_found:
        logger.debug(
            "Post-tool redaction: %d secret(s) found in tool output — session=%s, tool=%s, patterns=%s",
            len(secrets_found),
            session_id,
            tool,
            secrets_found,
        )

    _write_audit(audit, workdir)

    return EnforcementResult(
        redacted_output=redacted,
        audit=audit,
        should_block=dangerous,
    )


def redact_tool_output(output: str) -> str:
    """Redact secrets from a tool output string (no audit record written).

    Convenience helper for callers that just want the redacted text.

    Args:
        output: Raw tool output.

    Returns:
        Output with detected secret patterns replaced by ``[REDACTED]``.
    """
    result = output
    for _name, pattern in _OUTPUT_SECRET_PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


# ---------------------------------------------------------------------------
# Audit persistence
# ---------------------------------------------------------------------------


def _write_audit(audit: AuditRecord, workdir: Path | None) -> None:
    """Append an audit record to ``.sdd/metrics/tool_audit.jsonl``.

    Silent no-op if *workdir* is ``None`` or the path cannot be created.

    Args:
        audit: The audit record to persist.
        workdir: Project root directory.
    """
    if workdir is None:
        return
    try:
        metrics_dir = workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "timestamp": audit.timestamp,
            "session_id": audit.session_id,
            "tool": audit.tool,
            "raw_length": audit.raw_output_length,
            "redacted_length": audit.redacted_output_length,
            "secrets_found": audit.secrets_found,
            "dangerous": audit.dangerous,
            "blocked": audit.blocked,
        }
        with open(metrics_dir / "tool_audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.debug("Failed to write tool audit record: %s", exc)
