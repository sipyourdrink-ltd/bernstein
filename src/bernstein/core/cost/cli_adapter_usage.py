"""Per-call token-usage capture for plain CLI-adapter runs (issue #2797).

The openai_agents runner and the Claude Code wrapper each write a per-session
``.tokens`` sidecar *during* a run, which the completion path recovers via
:func:`bernstein.core.agents.agent_lifecycle._read_runner_cost_usd`. Plain CLI
adapters (qwen and friends) wrote no such sidecar and captured no usage, so
``bernstein cost`` reported ``Tokens In 0`` / ``Tokens Out 0`` for runs that
made real model calls - the whole usage view was blank on a free route, where
the ``$0`` dollar total is legitimately zero and token counts are the only
usage signal.

This module gives the CLI-adapter path its own usage source. The qwen adapter
requests ``--output-format stream-json``, whose session log carries the
provider's own token accounting (``stats.models[<route>].tokens`` and per-call
``usage`` blocks). :func:`capture_cli_adapter_usage` parses that log and writes
the same ``{"ts", "in", "out"}`` sidecar the recovery path already consumes, so
no downstream accounting code has to change to see real counts.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Token-count key spellings seen across qwen-code output surfaces. The
# authoritative ``stats.models[<route>].tokens`` breakdown uses
# ``prompt``/``completion``; per-call ``usage`` blocks use
# ``input_tokens``/``output_tokens`` (and a few provider-native aliases). We
# accept all of them so capture stays robust across model routes rather than
# pinned to one route's field names.
_INPUT_KEYS: tuple[str, ...] = (
    "prompt",
    "input_tokens",
    "prompt_tokens",
    "input",
    "promptTokenCount",
    "inputTokens",
)
_OUTPUT_KEYS: tuple[str, ...] = (
    "completion",
    "output_tokens",
    "completion_tokens",
    "output",
    "candidatesTokenCount",
    "outputTokens",
)


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return ``value`` as a ``dict[str, Any]`` when it is a mapping, else None."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _coerce_int(value: Any) -> int:
    """Return ``value`` as a non-negative int, or ``0`` when not coercible."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _extract_in_out(usage: dict[str, Any]) -> tuple[int, int]:
    """Extract ``(input, output)`` token counts from a usage/tokens dict."""
    tokens_in = 0
    tokens_out = 0
    for key in _INPUT_KEYS:
        if key in usage:
            tokens_in = _coerce_int(usage[key])
            break
    for key in _OUTPUT_KEYS:
        if key in usage:
            tokens_out = _coerce_int(usage[key])
            break
    return tokens_in, tokens_out


def _records_from_text(text: str) -> list[dict[str, Any]]:
    """Parse stream-json (line-delimited) or a single buffered JSON object.

    ``--output-format stream-json`` emits one JSON object per line;
    ``--output-format json`` emits a single buffered object (or array). Both
    are handled: whole-text parse first, then a per-line fallback.
    """
    text = text.strip()
    if not text:
        return []
    whole: Any = None
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return [cast("dict[str, Any]", whole)]
    if isinstance(whole, list):
        return [d for d in (_as_dict(rec) for rec in cast("list[Any]", whole)) if d is not None]

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed: Any = json.loads(line)
        except ValueError:
            continue
        rec = _as_dict(parsed)
        if rec is not None:
            records.append(rec)
    return records


def _models_tokens(models: dict[str, Any]) -> tuple[int, int, str]:
    """Sum ``tokens`` across a ``models`` map; return (in, out, first model)."""
    tokens_in = 0
    tokens_out = 0
    model = ""
    for name, entry in models.items():
        if not model and name:
            model = name
        entry_dict = _as_dict(entry)
        if entry_dict is not None:
            tokens = _as_dict(entry_dict.get("tokens"))
            if tokens is not None:
                call_in, call_out = _extract_in_out(tokens)
                tokens_in += call_in
                tokens_out += call_out
    return tokens_in, tokens_out, model


def _record_model(rec: dict[str, Any]) -> str:
    """Best-effort model/route id from a stream-json record."""
    model = rec.get("model")
    if isinstance(model, str) and model:
        return model
    message = _as_dict(rec.get("message"))
    if message is not None:
        inner = message.get("model")
        if isinstance(inner, str) and inner:
            return inner
    return ""


def _record_usage(rec: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``usage`` dict on a record or its nested message."""
    usage = _as_dict(rec.get("usage"))
    if usage is not None:
        return usage
    message = _as_dict(rec.get("message"))
    if message is not None:
        return _as_dict(message.get("usage"))
    return None


def parse_stream_json_usage(text: str) -> tuple[int, int, str]:
    """Parse qwen-code stream-json output into ``(in, out, model)`` tokens.

    Precedence, most authoritative first, so counts are never double-summed:

    1. ``stats.models`` / ``metrics.models`` - the provider's own cumulative
       per-session breakdown (``tokens.prompt`` / ``tokens.completion``).
    2. The terminal ``result`` message's cumulative ``usage`` block.
    3. The sum of per-call ``usage`` blocks on ``assistant`` messages.

    Args:
        text: Raw session-log text (line-delimited stream-json, or a single
            buffered JSON object/array).

    Returns:
        ``(input_tokens, output_tokens, model)``. All zero / empty when the
        text carries no recognisable usage.
    """
    records = _records_from_text(text)
    if not records:
        return 0, 0, ""

    fallback_model = ""
    for rec in records:
        fallback_model = _record_model(rec)
        if fallback_model:
            break

    # 1. Authoritative provider breakdown.
    for rec in records:
        for container_key in ("stats", "metrics"):
            container = _as_dict(rec.get(container_key))
            if container is None:
                continue
            models = _as_dict(container.get("models"))
            if models:
                tokens_in, tokens_out, model = _models_tokens(models)
                if tokens_in > 0 or tokens_out > 0:
                    return tokens_in, tokens_out, model or fallback_model

    # 2. Terminal result usage (cumulative for the session).
    for rec in records:
        if rec.get("type") == "result":
            usage = _record_usage(rec)
            if usage is not None:
                tokens_in, tokens_out = _extract_in_out(usage)
                if tokens_in > 0 or tokens_out > 0:
                    return tokens_in, tokens_out, _record_model(rec) or fallback_model

    # 3. Sum per-call assistant usage.
    total_in = 0
    total_out = 0
    model = ""
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        usage = _record_usage(rec)
        if usage is None:
            continue
        call_in, call_out = _extract_in_out(usage)
        total_in += call_in
        total_out += call_out
        if not model:
            model = _record_model(rec)
    if total_in > 0 or total_out > 0:
        return total_in, total_out, model or fallback_model

    return 0, 0, fallback_model


def _resolve_session_log(workdir: Path, session_id: str) -> Path | None:
    """Resolve the on-disk session log for ``session_id``.

    Mirrors the candidate order of
    :meth:`bernstein.core.agents.agent_log_aggregator.AgentLogAggregator._resolve_log_path`
    so worktree-isolated runs (where the adapter wrote its log inside a
    per-task worktree) are found alongside the non-isolated root layout.
    """
    candidates = (
        workdir / ".sdd" / "runtime" / f"{session_id}.log",
        workdir / ".sdd" / "logs" / f"{session_id}.log",
        workdir / ".sdd" / "worktrees" / session_id / ".sdd" / "runtime" / f"{session_id}.log",
        workdir / ".sdd" / "worktrees" / session_id / ".sdd" / "logs" / f"{session_id}.log",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def capture_cli_adapter_usage(
    workdir: Path,
    session_id: str,
    log_path: Path | None = None,
) -> tuple[int, int, str]:
    """Recover CLI-adapter token usage and materialise the recovery sidecar.

    Parses the adapter's structured session log and, when it carries real
    token counts, appends a ``{"ts", "in", "out"}`` record to the
    orchestrator-root ``.sdd/runtime/<session_id>.tokens`` sidecar - the exact
    file
    :func:`bernstein.core.agents.agent_lifecycle._read_runner_cost_usd` reads
    at task completion. No-op when a sidecar already exists (the openai_agents
    runner or the Claude wrapper wrote one during the run) so counts are never
    double-recorded.

    Args:
        workdir: Orchestrator root working directory. The sidecar is written
            under ``workdir/.sdd/runtime`` regardless of worktree isolation,
            so the completion-time recovery keyed off the same root finds it.
        session_id: Agent session id - the sidecar/log stem.
        log_path: Explicit session-log path (e.g. ``session.log_path``). When
            absent or missing, the workdir candidate layout is searched.

    Returns:
        ``(input_tokens, output_tokens, model)`` parsed from the log. All zero
        / empty when there is no sidecar to write (already present, log
        missing, or no usage found).
    """
    if not session_id:
        return 0, 0, ""

    sidecar_path = workdir / ".sdd" / "runtime" / f"{session_id}.tokens"
    try:
        if sidecar_path.exists() and sidecar_path.stat().st_size > 0:
            # A runner/wrapper already recorded usage during the run.
            return 0, 0, ""
    except OSError:
        pass

    resolved = log_path if (log_path is not None and log_path.exists()) else _resolve_session_log(workdir, session_id)
    if resolved is None:
        return 0, 0, ""
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0, ""

    tokens_in, tokens_out, model = parse_stream_json_usage(text)
    if tokens_in <= 0 and tokens_out <= 0:
        return 0, 0, model

    try:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps({"ts": time.time(), "in": tokens_in, "out": tokens_out})
        with sidecar_path.open("a", encoding="utf-8") as fh:
            fh.write(record + "\n")
    except OSError as exc:
        # "tokens" here is the usage-count sidecar file, not a credential.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.warning("failed to write CLI-adapter tokens sidecar %s: %s", sidecar_path, exc)

    return tokens_in, tokens_out, model
