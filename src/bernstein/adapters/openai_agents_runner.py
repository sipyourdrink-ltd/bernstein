"""Python entrypoint that runs an OpenAI Agents SDK session.

Bernstein's :class:`~bernstein.adapters.openai_agents.OpenAIAgentsAdapter`
launches this module as a subprocess (``python -m
bernstein.adapters.openai_agents_runner --manifest <path>``).  The
manifest file describes a single :class:`agents.Agent` invocation: the
model, prompt, tool list, sandbox provider, and optional MCP servers.

The runner imports the ``openai-agents`` package lazily so that simply
importing this module (e.g. for unit tests that stub ``Runner.run``) does
not require the SDK to be installed.  Missing SDK is treated as a hard
error only at :func:`run` time.

Output protocol
---------------

All output is line-delimited JSON written to ``stdout``.  Each event is a
single JSON object with a ``type`` field.  The spawner does not parse
events strictly - they are persisted to the session log and exposed via
the existing log tail + hooks plumbing - but the schema below is what
tests and downstream cost-tracking code rely on::

    {"type": "start", "session_id": "...", "model": "gpt-5-mini",
     "temperature": null, "top_p": null, "top_k": null,
     "base_url": null, "api_key_env": null}
    {"type": "tool_call", "name": "file_read", "args": {...}}
    {"type": "tool_result", "name": "file_read", "output": "..."}
    {"type": "progress", "message": "..."}
    {"type": "usage", "input_tokens": 123, "output_tokens": 456, "tool_calls": 3,
     "model": "gpt-5-mini", "cost_usd": 0.00123, "priced": true,
     "running_total_usd": 0.00123}
    {"type": "completion", "status": "done", "summary": "..."}
    {"type": "error", "message": "...", "kind": "rate_limit"}

Exit codes
----------

* ``0`` - completion event emitted successfully
* ``2`` - manifest missing or malformed, or ``api_key_env`` names an
  environment variable that is not set
* ``3`` - optional ``openai-agents`` SDK not installed
* ``4`` - provider rate-limit detected (maps to Bernstein's back-off)
* ``1`` - any other runtime error
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import itertools
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from bernstein.core.instrumentation import get_instrumenter, init_instrumenter, resolve_agent_dir
from bernstein.core.security.sanitize import sanitize_log

logger = logging.getLogger(__name__)

# Wave 3 (per-agent instrumentation): monotonically increasing ids for
# llm-calls.jsonl / tool-calls.jsonl records within this process. Reset per
# ``run()`` invocation via ``_reset_instrumentation_counters`` so repeated
# in-process invocations (e.g. tests that call ``run()`` more than once)
# don't accumulate call ids across sessions.
_llm_call_counter = itertools.count(1)
_tool_call_counter = itertools.count(1)

# FIFO of open tool_call starts, keyed by tool name. The SDK's event stream
# (``tool_call`` then ``tool_result``) carries no call id to correlate the
# two events, so this assumes same-name tool calls complete in the order
# they started (true for the synchronous, single-threaded builtin tool
# implementations wave 3 hooks - see openai_agents_builtins.py). A future
# wave that makes tool execution concurrent within one turn should have the
# event source emit an explicit id instead of relying on this ordering
# assumption (documented as a gap in the wave-3 final report).
_pending_tool_calls: dict[str, list[dict[str, Any]]] = {}

# Conversation message index, shared across the initial system/user messages
# logged before the SDK run and the post-hoc turns extracted from the
# result's ``new_items`` afterwards (see gap note in
# ``_log_result_conversation_messages``).
_conversation_idx_counter = itertools.count(0)

# Wave 4 (per-turn instrumentation, fixing the "only one aggregate llm-call
# entry per agent" bug): count of llm-calls.jsonl entries written by the
# SDK RunHooks-driven on_llm_end hook (see _build_instrumentation_hooks)
# during the CURRENT run() invocation. When this is > 0 at the end of the
# session, two pre-existing "single aggregate entry" code paths are
# deliberately SKIPPED so a run doesn't get N precise per-turn entries PLUS
# a duplicate aggregate/post-hoc one: (1) the "usage" branch of
# _instrument_event no longer mirrors an llm-call entry, and (2) the
# post-hoc _log_result_conversation_messages() call is skipped in favor of
# the real-time per-turn conversation logging the hook already did (see
# _log_hook_turn_conversation). Reset per run() via
# _reset_instrumentation_counters so a fresh invocation (e.g. a test
# calling run() twice) starts clean.
_hook_llm_calls_logged: int = 0


def _reset_instrumentation_counters() -> None:
    """Reset per-process instrumentation counters/state (test/re-run hygiene)."""
    global _llm_call_counter, _tool_call_counter, _conversation_idx_counter, _hook_llm_calls_logged
    _llm_call_counter = itertools.count(1)
    _tool_call_counter = itertools.count(1)
    _conversation_idx_counter = itertools.count(0)
    _hook_llm_calls_logged = 0
    _pending_tool_calls.clear()


def _mark_hook_llm_call_logged() -> None:
    """Record that the per-turn RunHooks path wrote one llm-calls.jsonl entry.

    Called exactly once per successful (or hook-detected-failed) turn from
    inside the dynamically-built ``RunHooks`` subclass in
    :func:`_build_instrumentation_hooks`. See :data:`_hook_llm_calls_logged`
    docstring for why downstream aggregate-logging paths check this.
    """
    global _hook_llm_calls_logged
    _hook_llm_calls_logged += 1


def _log_initial_conversation_messages(manifest: RunnerManifest) -> None:
    """Log the system addendum (if any) and the task prompt as messages 0/1.

    This is the ONLY part of this runner's conversation instrumentation that
    reflects true real-time message-append order: the openai-agents SDK does
    not expose its internal, turn-by-turn growing message list to the
    caller while ``Runner.run_sync`` is executing (it is entirely internal
    to the SDK's run loop) - only the final accumulated result afterwards.
    See :func:`_log_result_conversation_messages` for the post-hoc
    best-effort coverage of what happened during the run, and the wave-3
    final report for this documented gap.
    """
    instrumenter = get_instrumenter()
    if manifest.system_addendum:
        logger.debug(
            "_log_initial_conversation_messages: logging system_addendum, length=%d",
            len(manifest.system_addendum),
        )
        instrumenter.log_message(
            idx=next(_conversation_idx_counter),
            role="system",
            content_length=len(manifest.system_addendum),
            content=manifest.system_addendum,
        )
    logger.debug("_log_initial_conversation_messages: logging user prompt, length=%d", len(manifest.prompt))
    instrumenter.log_message(
        idx=next(_conversation_idx_counter),
        role="user",
        content_length=len(manifest.prompt),
        content=manifest.prompt,
    )


def _log_result_conversation_messages(result: Any) -> None:
    """Best-effort post-hoc conversation logging from the SDK's ``RunResult``.

    ``Runner.run_sync`` returns ``new_items`` - the SDK's own list of every
    item (message, tool call, tool output, ...) generated during the run.
    Reading it AFTER completion is the only place the accumulated
    conversation is available at all (see
    :func:`_log_initial_conversation_messages`'s docstring) - the
    ``ts`` recorded here is therefore "when we logged it", not "when the SDK
    actually appended it", and the whole run's turns appear at once rather
    than incrementally. Every attribute access is defensive (``getattr``
    with a default) since ``new_items`` element shapes vary by SDK version
    and item type, and a council run's ``result`` has no ``new_items`` at
    all (each member's own sub-result is not surfaced here - out of scope
    for this best-effort pass).
    """
    try:
        new_items = getattr(result, "new_items", None) or []
    except Exception as exc:
        logger.warning("_log_result_conversation_messages: failed to read new_items: %s", exc)
        return

    instrumenter = get_instrumenter()
    for item in new_items:
        try:
            raw_item = getattr(item, "raw_item", item)
            item_type = type(item).__name__
            role = str(getattr(raw_item, "role", None) or _infer_role_from_item_type(item_type))
            content = getattr(raw_item, "content", None)
            tool_name = getattr(raw_item, "name", None)
            content_text = str(content) if content is not None else str(raw_item)
            content_length = len(content_text)
            instrumenter.log_message(
                idx=next(_conversation_idx_counter),
                role=role,
                content_length=content_length,
                content=content_text,
                tool_calls=[str(tool_name)] if tool_name else None,
            )
        except Exception as exc:
            logger.warning("_log_result_conversation_messages: skipped one item (%s): %s", type(item).__name__, exc)

    try:
        final_output = getattr(result, "final_output", None)
        if final_output is not None:
            final_output_text = str(final_output)
            instrumenter.log_message(
                idx=next(_conversation_idx_counter),
                role="assistant",
                content_length=len(final_output_text),
                content=final_output_text,
            )
    except Exception as exc:
        logger.warning("_log_result_conversation_messages: failed to log final_output: %s", exc)


def _infer_role_from_item_type(item_type: str) -> str:
    """Map an SDK ``RunItem`` subclass name onto a role label for logging."""
    lowered = item_type.lower()
    if "tool" in lowered:
        return "tool"
    if "message" in lowered or "output" in lowered:
        return "assistant"
    return "unknown"


def _log_hook_turn_conversation(response: Any) -> None:
    """Log one conversation.jsonl entry per output item generated THIS turn.

    Called from the per-turn ``on_llm_end`` hook (see
    :func:`_build_instrumentation_hooks`) with the SDK's ``ModelResponse``
    for that single LLM call. Unlike :func:`_log_result_conversation_messages`
    (the old best-effort path - still used as a fallback for runs where the
    hooks path never engaged, e.g. a task-level council run, see the
    ``_hook_llm_calls_logged`` guards in :func:`_run_session`), this fires
    in REAL TIME as each turn completes rather than once, after-the-fact,
    for the whole session - fixing the "conversation.jsonl only shows a
    final summary" half of this wave's bug report.

    Wrapped defensively exactly like its post-hoc sibling: a malformed or
    unexpected ``response.output`` item shape must never crash the agent
    run being observed.
    """
    instrumenter = get_instrumenter()
    try:
        output_items = getattr(response, "output", None) or []
    except Exception as exc:
        logger.warning("_log_hook_turn_conversation: failed to read response.output: %s", exc)
        return

    for item in output_items:
        try:
            item_type = type(item).__name__
            role = str(getattr(item, "role", None) or _infer_role_from_item_type(item_type))
            content = getattr(item, "content", None)
            tool_name = getattr(item, "name", None)
            content_text = str(content) if content is not None else str(item)
            content_length = len(content_text)
            instrumenter.log_message(
                idx=next(_conversation_idx_counter),
                role=role,
                content_length=content_length,
                content=content_text,
                tool_calls=[str(tool_name)] if tool_name else None,
            )
        except Exception as exc:
            logger.warning(
                "_log_hook_turn_conversation: skipped one item (%s): %s",
                type(item).__name__,
                exc,
            )


def _model_name_for_hooks(agent: Any, manifest: RunnerManifest) -> str:
    """Best-effort real model id for a per-turn hook's llm-calls.jsonl entry.

    ``agent.model`` is either a plain string (the common case) or an SDK
    ``Model`` instance - e.g. ``OpenAIChatCompletionsModel``, built by the
    ``explicit_model_client`` branch in :func:`_run_session` for
    ``base_url``-overridden endpoints - which itself exposes the underlying
    model id as its OWN ``.model`` attribute (confirmed via on-disk
    inspection of ``agents/models/openai_chatcompletions.py``:
    ``self.model = model`` in ``__init__``). Falls back to
    ``manifest.model`` and never raises, so a model-name lookup can never
    crash the run a hook is observing.
    """
    try:
        model_attr = getattr(agent, "model", None)
        if isinstance(model_attr, str) and model_attr:
            return model_attr
        if model_attr is not None:
            inner = getattr(model_attr, "model", None)
            if isinstance(inner, str) and inner:
                return inner
    except Exception as exc:
        logger.warning(
            "_model_name_for_hooks: failed to resolve model name for session=%s, falling back to manifest.model=%r: %s",
            manifest.session_id,
            manifest.model,
            exc,
        )
    return manifest.model


def _build_instrumentation_hooks(sdk: Any, manifest: RunnerManifest) -> Any:
    """Build a per-run ``RunHooks`` instance wired to the ``RunInstrumenter``.

    This is the wave-4 fix for "only one aggregate llm-call entry gets
    logged per agent instead of one entry per actual per-turn LLM call".
    On-disk inspection of the installed SDK (openai-agents 0.17.7,
    ``agents/lifecycle.py``) shows ``RunHooksBase`` exposes
    ``on_llm_start``/``on_llm_end`` firing once PER MODEL CALL (turn) with
    that turn's ``ModelResponse`` (usage, output items), and
    ``on_tool_start``/``on_tool_end`` firing once PER TOOL CALL with a
    ``ToolContext`` carrying ``tool_call_id``/``tool_name``/
    ``tool_arguments`` - exactly the granularity llm-calls.jsonl and
    tool-calls.jsonl are supposed to have. ``Runner.run_sync`` already
    accepts a ``hooks: RunHooks[TContext] | None`` kwarg (see its signature
    in ``agents/run.py``) that the SDK threads straight through its
    internal turn loop, so no switch to ``Runner.run_streamed()`` is
    needed - wiring ``hooks=`` into the existing ``run_sync_kwargs`` in
    :func:`_run_session` is sufficient.

    Tool-call hook logging is deliberately SKIPPED when
    ``manifest.tool_source == "builtin"``:
    :mod:`bernstein.adapters.openai_agents_builtins` already emits its own
    ``tool_call``/``tool_result`` stdout events for the four
    workdir-sandboxed builtins, which :func:`_instrument_event` already
    mirrors into tool-calls.jsonl via the ``_pending_tool_calls`` FIFO.
    Wiring the hook unconditionally would double-log every builtin tool
    call (once from the builtin's own emit_event, once from the hook).
    Gateway-sourced tools (``tool_source == "gateway"``, the default) have
    NO other tool-call logging path today, so the hook is the ONLY source
    of tool-calls.jsonl coverage for them.

    Args:
        sdk: The imported ``agents`` module (already cast to ``Any`` by the
            caller in :func:`_run_session`).
        manifest: The parsed runner manifest for this session.

    Returns:
        An instance of a dynamically-created ``sdk.RunHooks`` subclass.
        Built dynamically (not a module-level class) because ``sdk.RunHooks``
        - a generic alias over the lazily-imported SDK's own base class -
        only exists once the optional SDK has actually been imported.
    """
    instrumenter = get_instrumenter()
    log_tool_hooks = manifest.tool_source != "builtin"
    logger.debug(
        "hook registration: building instrumentation RunHooks for session=%s "
        "tool_source=%r (tool-call hook logging %s)",
        manifest.session_id,
        manifest.tool_source,
        "enabled" if log_tool_hooks else "disabled - builtin tools log via their own emit_event path",
    )

    class _InstrumentationHooks(sdk.RunHooks):  # type: ignore[misc,valid-type]
        """Dynamically-built RunHooks subclass - see enclosing factory docstring."""

        def __init__(self) -> None:
            super().__init__()
            self._llm_call_id: str | None = None
            self._llm_ts_start: str | None = None
            self._pending_tools: dict[str, dict[str, Any]] = {}

        async def on_llm_start(
            self,
            context: Any,
            agent: Any,
            system_prompt: Any,
            input_items: Any,
        ) -> None:
            try:
                call_id = f"c-{next(_llm_call_counter)}"
                self._llm_call_id = call_id
                self._llm_ts_start = _now_iso_for_instrumentation()
                logger.debug(
                    "hook fired: on_llm_start call_id=%s session=%s model=%s input_items=%d",
                    call_id,
                    manifest.session_id,
                    _model_name_for_hooks(agent, manifest),
                    len(input_items) if input_items is not None else 0,
                )
            except Exception as exc:
                logger.warning("on_llm_start hook failed for session=%s: %s", manifest.session_id, exc)

        async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
            ts_end = _now_iso_for_instrumentation()
            call_id = self._llm_call_id or f"c-{next(_llm_call_counter)}"
            ts_start = self._llm_ts_start or ts_end
            self._llm_call_id = None
            self._llm_ts_start = None
            try:
                model_name = _model_name_for_hooks(agent, manifest)
                usage = getattr(response, "usage", None)
                prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else None
                completion_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else None
                total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else None
                instrumenter.log_llm_call(
                    call_id=call_id,
                    ts_start=ts_start,
                    ts_end=ts_end,
                    model=model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    status="ok",
                )
                _mark_hook_llm_call_logged()
                logger.debug(
                    "wrote llm_call entry call_id=%s session=%s model=%s usage_prompt=%s "
                    "usage_completion=%s usage_total=%s",
                    call_id,
                    manifest.session_id,
                    model_name,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                )
                _log_hook_turn_conversation(response)
            except Exception as exc:
                logger.warning(
                    "on_llm_end hook failed for session=%s call_id=%s: %s - logging a "
                    "status=error placeholder entry so this turn is not silently missing "
                    "from llm-calls.jsonl",
                    manifest.session_id,
                    call_id,
                    exc,
                )
                try:
                    instrumenter.log_llm_call(
                        call_id=call_id,
                        ts_start=ts_start,
                        ts_end=ts_end,
                        model=manifest.model,
                        status="error",
                        error=f"on_llm_end hook failed: {type(exc).__name__}: {exc}",
                    )
                    _mark_hook_llm_call_logged()
                except Exception as inner_exc:
                    logger.warning(
                        "on_llm_end hook: even the status=error fallback log_llm_call "
                        "failed for session=%s call_id=%s: %s",
                        manifest.session_id,
                        call_id,
                        inner_exc,
                    )

        async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
            if not log_tool_hooks:
                return
            try:
                tool_name = str(getattr(tool, "name", "unknown"))
                tool_call_id = str(getattr(context, "tool_call_id", None) or f"unkeyed-{next(_tool_call_counter)}")
                call_id = f"tc-{next(_tool_call_counter)}"
                raw_args = getattr(context, "tool_arguments", None)
                args_dict: dict[str, Any]
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                        args_dict = parsed_args if isinstance(parsed_args, dict) else {"args": parsed_args}
                    except (json.JSONDecodeError, TypeError):
                        args_dict = {"raw_args": raw_args}
                elif isinstance(raw_args, dict):
                    args_dict = raw_args
                else:
                    args_dict = {}
                self._pending_tools[tool_call_id] = {
                    "call_id": call_id,
                    "ts_start": _now_iso_for_instrumentation(),
                    "tool": tool_name,
                    "args": args_dict,
                }
                # tool_name/tool_call_id originate in the model's response
                # stream - sanitize before logging (established repo pattern
                # for externally-derived values).
                logger.debug(
                    "hook fired: on_tool_start call_id=%s tool=%s tool_call_id=%s session=%s",
                    call_id,
                    sanitize_log(tool_name),
                    sanitize_log(tool_call_id),
                    manifest.session_id,
                )
            except Exception as exc:
                logger.warning("on_tool_start hook failed for session=%s: %s", manifest.session_id, exc)

        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
            if not log_tool_hooks:
                return
            ts_end = _now_iso_for_instrumentation()
            try:
                tool_name = str(getattr(tool, "name", "unknown"))
                tool_call_id = str(getattr(context, "tool_call_id", None) or "")
                pending = self._pending_tools.pop(tool_call_id, None)
                call_id = pending["call_id"] if pending else f"tc-{next(_tool_call_counter)}"
                ts_start = pending["ts_start"] if pending else ts_end
                args = pending["args"] if pending else None
                is_error = isinstance(result, BaseException)
                # Bug fix (instrumentation audit, bug 2): tool-calls.jsonl
                # previously never recorded what a tool actually returned -
                # only its name/args/success flag - making it impossible to
                # see tool output without re-running the agent. The SDK's
                # on_tool_end hook receives the tool's return value directly
                # as `result` (a BaseException on failure per the is_error
                # check above, otherwise whatever the tool function
                # returned - str, dict, etc.); RunInstrumenter.log_tool_call
                # stringifies + truncates it. On the error path pass None
                # (the error string itself is already captured in the
                # `error` field) rather than duplicating the exception text.
                result_for_log = None if is_error else result
                logger.debug(
                    "on_tool_end: tool=%s call_id=%s is_error=%s result_type=%s",
                    tool_name,
                    call_id,
                    is_error,
                    type(result).__name__,
                )
                instrumenter.log_tool_call(
                    call_id=call_id,
                    ts_start=ts_start,
                    ts_end=ts_end,
                    tool=tool_name,
                    args=args,
                    success=not is_error,
                    error=f"{type(result).__name__}: {result}" if is_error else None,
                    result=result_for_log,
                )
                logger.debug(
                    "wrote tool_call entry call_id=%s tool=%s session=%s success=%s",
                    call_id,
                    sanitize_log(tool_name),
                    manifest.session_id,
                    not is_error,
                )
            except Exception as exc:
                logger.warning("on_tool_end hook failed for session=%s: %s", manifest.session_id, exc)

    return _InstrumentationHooks()


def _instrument_event(event: Mapping[str, Any]) -> None:
    """Best-effort translation of a runner stdout event into instrumentation records.

    Called from :func:`emit_event` for every event this runner already emits
    - it never changes what gets written to stdout, only additionally mirrors
    a subset of events into the active :class:`RunInstrumenter` (a no-op
    singleton until :func:`bernstein.core.instrumentation.init_instrumenter`
    has been called, e.g. before any SDK work starts in :func:`run`).

    Wrapped in a broad ``try/except`` so a malformed/unexpected event shape
    can never break the actual event stream this function is piggybacking
    on - see module docstring on :mod:`bernstein.core.instrumentation`.
    """
    try:
        event_type = event.get("type")
        instrumenter = get_instrumenter()
        now = _now_iso_for_instrumentation()

        if event_type == "usage":
            if _hook_llm_calls_logged > 0:
                # Wave 4: the per-turn RunHooks path (_build_instrumentation_hooks)
                # already wrote one llm-calls.jsonl entry per real LLM call this
                # session. This aggregate "usage" event is still needed for
                # stdout/cost-metering (_emit_session_usage), but mirroring it
                # into llm-calls.jsonl too would append a stale duplicate
                # aggregate entry on top of the N precise per-turn ones.
                logger.debug(
                    "_instrument_event: skipping aggregate llm-call mirror for usage "
                    "event - %d precise per-turn entries already logged via RunHooks "
                    "this session",
                    _hook_llm_calls_logged,
                )
                return
            call_id = f"c-{next(_llm_call_counter)}"
            usage_missing = bool(event.get("usage_missing"))
            instrumenter.log_llm_call(
                call_id=call_id,
                ts_start=now,
                ts_end=now,
                model=str(event.get("model", "")),
                prompt_tokens=cast("int | None", event.get("input_tokens")),
                completion_tokens=cast("int | None", event.get("output_tokens")),
                status="error" if usage_missing else "ok",
                error="usage_missing: SDK returned no usable token counts" if usage_missing else None,
            )
            # ts_start == ts_end here because the SDK only exposes an
            # AGGREGATE usage total for the whole session/council-member
            # (see ``_emit_session_usage``'s docstring), not per-call
            # wall-clock timing - wall_ms would be fabricated if computed
            # from these two identical timestamps, so callers reading
            # llm-calls.jsonl for this adapter should treat wall_ms==0 as
            # "unknown", not "instant". This is a documented gap - see the
            # wave-3 final report.
            return

        if event_type == "tool_call":
            name = str(event.get("name", "unknown"))
            call_id = f"tc-{next(_tool_call_counter)}"
            _pending_tool_calls.setdefault(name, []).append(
                {"call_id": call_id, "ts_start": now, "args": event.get("args")}
            )
            return

        if event_type == "tool_result":
            name = str(event.get("name", "unknown"))
            pending_list = _pending_tool_calls.get(name)
            pending = pending_list.pop(0) if pending_list else None
            call_id = pending["call_id"] if pending else f"tc-{next(_tool_call_counter)}"
            ts_start = pending["ts_start"] if pending else now
            args = pending["args"] if pending else None
            is_error = bool(event.get("is_error")) or bool(event.get("error"))
            # Bug fix (instrumentation audit, bug 2): mirror whatever
            # result-shaped data the emitting event carries. Today's builtin
            # tool emitters (openai_agents_builtins.py) only send metadata
            # (bytes/count), never the actual content, so this is best-effort
            # and frequently None for builtin-sourced events - the primary
            # fix for gateway-sourced tools is the on_tool_end hook path
            # above, which DOES see the tool's real return value.
            result_value = event.get("result") if "result" in event else event.get("output")
            if result_value is None and not is_error:
                # Fall back to whatever non-standard metadata keys this event
                # carries (e.g. bytes/count) so SOMETHING beyond
                # success/failure survives to tool-calls.jsonl even without
                # a first-class result payload.
                metadata_preview = {
                    k: v for k, v in event.items() if k not in {"type", "name", "tool_source", "status", "error"}
                }
                if metadata_preview:
                    result_value = metadata_preview
            instrumenter.log_tool_call(
                call_id=call_id,
                ts_start=ts_start,
                ts_end=now,
                tool=name,
                args=args if isinstance(args, dict) else {"args": args},
                success=not is_error,
                error=str(event.get("error")) if event.get("error") else None,
                result=result_value,
            )
            return
    except Exception as exc:
        logger.warning("_instrument_event failed for event type=%r: %s", event.get("type"), exc)


def _now_iso_for_instrumentation() -> str:
    """Local import-free ISO timestamp helper (mirrors instrumentation._now_iso)."""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds")


# Exit codes are part of the public contract with the adapter - keep in sync
# with the module docstring above.
EXIT_OK: int = 0
EXIT_GENERIC: int = 1
EXIT_MANIFEST_ERROR: int = 2
EXIT_SDK_MISSING: int = 3
EXIT_RATE_LIMIT: int = 4

# Env var overriding AGENT.max_turns (tuning.agent.max_turns in bernstein.yaml).
# Checked after the manifest value; an unset/blank/unparseable value falls
# through to the yaml-tunable default, which itself defaults to 30 (bug 13:
# the SDK's own default of 10 killed builtin-tool workflows mid-flight).
MAX_TURNS_ENV_VAR: str = "BERNSTEIN_MAX_TURNS"


def _resolve_max_turns(manifest_value: int | None = None) -> int | None:
    """Resolve the effective ``max_turns`` for ``Runner.run_sync``.

    Precedence: manifest ``max_turns`` (resolved by the spawn side, which
    sees the operator yaml tuning and the un-filtered environment) >
    ``BERNSTEIN_MAX_TURNS`` env var (direct-invocation fallback; the
    spawner's filtered env strips it for adapter-spawned runners) >
    ``AGENT.max_turns`` (yaml ``tuning.agent.max_turns``) > ``None``
    (SDK default applies - unchanged behavior for anyone not opting in).

    Logs the effective value and its source at INFO so a wrong turn cap
    (e.g. the SDK's default 10 killing a multi-tool run) is diagnosable
    from the runner log alone.
    """
    if manifest_value is not None:
        if isinstance(manifest_value, int) and manifest_value > 0:
            logger.info("max_turns=%d (source=manifest)", manifest_value)
            return manifest_value
        logger.warning(
            "manifest max_turns=%r must be a positive int; falling back to env/tuning/SDK default",
            manifest_value,
        )
    raw_env = os.environ.get(MAX_TURNS_ENV_VAR)
    if raw_env is not None and raw_env.strip():
        try:
            parsed = int(raw_env)
        except ValueError:
            logger.warning(
                "%s=%r is not a valid int; falling back to tuning.agent.max_turns/SDK default",
                MAX_TURNS_ENV_VAR,
                raw_env,
            )
        else:
            if parsed > 0:
                logger.info("max_turns=%d (source=env %s)", parsed, MAX_TURNS_ENV_VAR)
                return parsed
            logger.warning(
                "%s=%r must be a positive int; falling back to tuning.agent.max_turns/SDK default",
                MAX_TURNS_ENV_VAR,
                raw_env,
            )

    from bernstein.core.defaults import AGENT

    if AGENT.max_turns is not None:
        logger.info("max_turns=%d (source=default tuning.agent.max_turns)", AGENT.max_turns)
    else:
        logger.info("max_turns not configured (source=SDK default)")
    return AGENT.max_turns


# ``api_key_env`` must name a known LLM-provider credential.  The name both
# widens the filtered environment handed to this subprocess and selects the
# secret sent as the bearer key to ``base_url``, so an unconstrained value
# would let a repo-carried config forward arbitrary host secrets
# (``GITHUB_TOKEN``, ``AWS_SESSION_TOKEN``, ...) to an arbitrary endpoint.
# Fail-closed: names outside the built-in provider set are rejected unless
# the OPERATOR allows them via ``BERNSTEIN_ALLOWED_API_KEY_ENVS`` on the
# host (a repo-carried config cannot set host environment variables, so the
# override cannot be forged by the repo).  Keep in sync with the constraint
# documented in ``docs/adapters/capability_contract.md``.
_API_KEY_ENV_RE: re.Pattern[str] = re.compile(r"^[A-Z][A-Z0-9_]*$")
_API_KEY_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "TOGETHER_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "FIREWORKS_API_KEY",
        "XAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "CEREBRAS_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "NVIDIA_API_KEY",
        "PERPLEXITY_API_KEY",
        "HF_TOKEN",
        "LLM_API_KEY",
    }
)
_ALLOWED_API_KEY_ENVS_VAR = "BERNSTEIN_ALLOWED_API_KEY_ENVS"


def _operator_allowed_api_key_envs() -> frozenset[str]:
    """Extra credential names the operator allowed on this host.

    Read from the comma-separated ``BERNSTEIN_ALLOWED_API_KEY_ENVS``
    host environment variable. Names are stripped; empty entries are
    ignored.
    """
    raw = os.environ.get(_ALLOWED_API_KEY_ENVS_VAR, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def validate_api_key_env_name(name: str) -> None:
    """Reject ``api_key_env`` values outside the credential allowlist.

    Accepted names match ``^[A-Z][A-Z0-9_]*$`` AND are either in the
    built-in LLM-provider allowlist or explicitly allowed by the
    operator via ``BERNSTEIN_ALLOWED_API_KEY_ENVS`` (comma-separated,
    host-set). Everything else - including credential-shaped names of
    unrelated secrets such as ``GITHUB_TOKEN`` - is rejected so a
    repo-carried config cannot forward arbitrary host secrets to an
    arbitrary ``base_url``.

    Args:
        name: Candidate environment variable name from the manifest.

    Raises:
        RuntimeError: The name does not satisfy the constraint above.
    """
    if _API_KEY_ENV_RE.match(name) and (name in _API_KEY_ENV_ALLOWLIST or name in _operator_allowed_api_key_envs()):
        return
    msg = (
        f"api_key_env {name!r} is not an allowed credential variable name: "
        f"it must match ^[A-Z][A-Z0-9_]*$ and be a known LLM provider key, "
        f"or be explicitly allowed by the operator via "
        f"{_ALLOWED_API_KEY_ENVS_VAR} (comma-separated env var names)."
    )
    raise RuntimeError(msg)


@dataclass(frozen=True)
class RunnerManifest:
    """Typed view of the JSON manifest written by the adapter.

    Attributes:
        session_id: Bernstein session identifier for log correlation.
        prompt: Task prompt forwarded verbatim to ``Runner.run``.
        workdir: Absolute path to the worktree the sandbox must be
            constrained to.
        model: OpenAI model ID (e.g. ``"gpt-5"``, ``"gpt-5-mini"``).
        effort: Effort tier ("low", "medium", "high", "max").
        max_tokens: Per-run token cap for the underlying completion call.
        timeout_seconds: Wall-clock timeout forwarded to the SDK runner.
        task_scope: Scope label used for budget calculations.
        budget_multiplier: Retry multiplier applied to the scope budget.
        system_addendum: Extra system-prompt lines (completion protocol,
            signal-check, heartbeat) injected by the orchestrator.
        sandbox_provider: One of ``unix_local``, ``docker``, ``e2b``,
            ``modal``.  The runner maps this onto the SDK's
            ``SandboxRunConfig`` client.
        tools: Normalized tool descriptors from the Bernstein MCP gateway.
            The runner translates each entry into an SDK ``Tool``.
        tool_source: Where the agent's tools come from. ``"gateway"``
            (default) uses the MCP-gateway descriptors in ``tools`` exactly
            as before. ``"builtin"`` opts into the four workdir-sandboxed
            builtins (``read_file``, ``write_file``, ``list_dir``,
            ``run_command``) so a run with no MCP gateway reachable still
            has an audited way to act on the workdir. Any other value falls
            back to ``"gateway"``.
        mcp_servers: MCP servers Bernstein already manages.  Forwarded to
            the SDK so the Agent can call into them *without* letting the
            SDK spawn its own server processes (avoids duplicate
            connections and double cost accounting).
        temperature: Optional sampling temperature forwarded to the SDK's
            ``ModelSettings``.  ``None`` keeps the provider default.
        top_p: Optional nucleus-sampling value forwarded to
            ``ModelSettings``.  ``None`` keeps the provider default.
        top_k: Optional top-k sampling value forwarded to ``ModelSettings``
            via ``extra_args`` (the OpenAI API itself has no ``top_k``, but
            OpenAI-compatible endpoints selected via ``base_url`` do).
            ``None`` keeps the provider default.
        base_url: Optional OpenAI-compatible endpoint URL.  When set the
            runner constructs its own ``AsyncOpenAI`` client instead of the
            SDK default, switches the SDK to the chat-completions API
            (third-party endpoints do not serve ``/responses``), and
            excludes the client from tracing so the endpoint's key is
            never sent to api.openai.com.  ``None`` keeps today's
            single-endpoint behavior.
        api_key_env: Optional NAME of the environment variable holding the
            API key for ``base_url``.  Never a literal key.  Must satisfy
            :func:`validate_api_key_env_name`.  When set but the variable
            is missing (or the name is rejected) the runner fails at
            startup with :data:`EXIT_MANIFEST_ERROR`.
        heartbeat_dir: Optional absolute path of the heartbeat directory
            the orchestrator's ``HeartbeatMonitor`` watches (the project
            root's ``.sdd/runtime/heartbeats``).  Set by the spawner
            because ``workdir`` is a per-session worktree under default
            isolation - a worktree-relative heartbeat would never be
            observed.  When absent the runner falls back to
            ``<workdir>/.sdd/runtime/heartbeats``.
    """

    session_id: str
    prompt: str
    workdir: str
    model: str
    effort: str = "high"
    max_tokens: int = 200_000
    timeout_seconds: int = 1800
    task_scope: str = "medium"
    budget_multiplier: float = 1.0
    system_addendum: str = ""
    sandbox_provider: str = "unix_local"
    tools: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    tool_source: str = "gateway"
    mcp_servers: dict[str, Any] = field(default_factory=dict[str, Any])
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    heartbeat_dir: str | None = None
    # Wave 3 (per-agent instrumentation): Bernstein task id this session is
    # working, injected by ``spawner_core`` (mirrors ``heartbeat_dir`` -
    # resolved on the SPAWN side, since the runner has no other way to know
    # which orchestrator task it belongs to). ``None`` when absent (e.g. a
    # hand-written manifest for a direct invocation/test) - the runner then
    # instruments under a literal "unknown" task bucket rather than failing.
    task_id: str | None = None
    # Bug fix (instrumentation audit, bug 3 - "4 of 9 implement tasks have
    # zero instrumentation"): when spawner_core batches multiple Bernstein
    # tasks onto ONE agent process (``_spawn_for_tasks_internal``'s
    # ``tasks: list[Task]``), only ``tasks[0].id`` was ever threaded through
    # as ``task_id`` above - every OTHER task in the batch got no
    # instrumentation directory at all, since a single RunInstrumenter only
    # ever knew about one task_id. ``task_ids`` carries the FULL batch (all
    # task ids this agent process is working, ``task_id`` included) so
    # :func:`run` can fan the same instrumentation out to every task's
    # ``.sdd/runs/<run_id>/tasks/<task_id>/agents/<agent_id>/`` directory,
    # not just the first. ``None``/empty on hand-written manifests and on
    # single-task spawns (the common case) - the runner then falls back to
    # the single ``task_id`` dir exactly as before.
    task_ids: list[str] | None = None
    # Wave 3 (per-agent instrumentation): orchestrator-root directory,
    # injected by ``spawner_core`` (mirrors ``heartbeat_dir`` above - same
    # reasoning: ``workdir`` is a per-session worktree under default
    # isolation, deleted on cleanup/merge, so instrumentation JSONL must be
    # anchored to the project root the orchestrator itself uses for
    # ``.sdd/runs/<run_id>/summary.json`` - see
    # :func:`bernstein.core.orchestration.run_report.write_summary_json`).
    # ``None`` when absent (e.g. a hand-written manifest for a direct
    # invocation/test) - the runner then falls back to ``workdir``, which is
    # also correct for those callers since there is no separate worktree.
    instrumentation_root: str | None = None
    # Control knobs resolved by the SPAWN side. They must travel in the
    # manifest because the spawner hands the runner a filtered environment
    # (env_isolation) that strips BERNSTEIN_* control vars - parent-env
    # values never reach this subprocess on their own. ``None`` means the
    # manifest did not carry the field (e.g. a hand-written manifest for a
    # direct invocation) and the runner's own env/defaults apply.
    allow_run_command: bool | None = None
    max_turns: int | None = None
    # Optional TASK-LEVEL "council of agents" fan-out/judge override (see
    # ``bernstein.adapters.council_runner.run_council`` and
    # ``bernstein.core.config.config_schema.CouncilConfig``). Shape::
    #
    #     {"candidates": [{"model": "...", "base_url": "...", "api_key_env": "..."}, ...],
    #      "judge": {"model": "...", "base_url": "...", "api_key_env": "..."},
    #      "timeout": 60.0}
    #
    # ``base_url``/``api_key_env`` are optional per-member endpoint
    # overrides, resolved through the exact same
    # ``_resolve_client_kwargs``/``validate_api_key_env_name`` path as the
    # top-level ``base_url``/``api_key_env`` fields above.
    #
    # This field is usually populated INDIRECTLY: when ``model`` (above)
    # names a ``.yaml``/``.yml`` file (the ``role_model_policy.<role>.model:
    # "councils/foo.yaml"`` convention), :func:`_load_council_config`
    # resolves that file relative to ``.bernstein/`` in ``workdir``, parses
    # it into this exact shape, and :func:`run` rebuilds the manifest with
    # this field populated before ``_run_session`` runs. A caller MAY also
    # set this field directly (e.g. a hand-written manifest for a direct
    # invocation/test) - it is then used as-is and ``model``'s ``.yaml``
    # suffix is not re-checked.
    #
    # When set (by either path), each candidate runs the WHOLE task to
    # completion independently (its own full ``Runner.run``), then a judge
    # model synthesizes one improved answer from every candidate's final
    # output - see :func:`run_council <bernstein.adapters.council_runner.run_council>`
    # for the task-level implementation the ``manifest.council`` branch in
    # :func:`_run_session` calls instead of ``Runner.run_sync``. ``None``
    # (the default) preserves today's single-model behavior exactly.
    council: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RunnerManifest:
        """Build a manifest from the parsed JSON dict.

        Unknown keys are ignored so forward-compatible adapter changes
        do not break older runner builds.
        """
        allowed_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in allowed_fields}
        return cls(**filtered)


def emit_event(event: Mapping[str, Any]) -> None:
    """Write a single JSON event to stdout.

    Each event is written on its own line and ``stdout`` is flushed so
    the spawner's log tail sees events in real time instead of only when
    the subprocess exits.

    Args:
        event: Mapping to serialize.  Must be JSON-encodable.
    """
    try:
        line = json.dumps(event, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        line = json.dumps({"type": "error", "message": f"non-serializable event: {exc}"})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    # Wave 3 (per-agent instrumentation): mirror a subset of events
    # (usage -> llm-calls.jsonl, tool_call/tool_result -> tool-calls.jsonl)
    # into the active RunInstrumenter. This never changes what was just
    # written to stdout above - _instrument_event is separately
    # try/except-wrapped and cannot raise.
    _instrument_event(event)


def load_manifest(path: Path) -> RunnerManifest:
    """Read and parse the manifest file written by the adapter.

    Args:
        path: Absolute path to the JSON manifest.

    Returns:
        A :class:`RunnerManifest` instance.

    Raises:
        FileNotFoundError: Manifest file does not exist.
        json.JSONDecodeError: File contents are not valid JSON.
        TypeError: JSON root is not a mapping.
    """
    raw_text = path.read_text(encoding="utf-8")
    parsed: object = json.loads(raw_text)
    if not isinstance(parsed, dict):
        msg = f"manifest root must be a JSON object, got {type(parsed).__name__}"
        raise TypeError(msg)
    return RunnerManifest.from_dict(cast("dict[str, Any]", parsed))


def _sdk_missing_message() -> str:
    """Return a human-readable hint when the SDK is not installed."""
    return (
        "openai-agents SDK is not installed. Reinstall bernstein with the "
        "`openai` extra: `pip install 'bernstein[openai]'`."
    )


def _is_rate_limit(exc: BaseException) -> bool:
    """Best-effort detection of provider-side rate limiting.

    The SDK raises provider-specific exceptions that we cannot import at
    adapter load time (the SDK is optional).  Instead, inspect the class
    name and message for the usual OpenAI rate-limit signals.  Callers
    should treat a ``True`` result as a reason to exit with
    :data:`EXIT_RATE_LIMIT`, which maps to Bernstein's existing back-off.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    needles = (
        "ratelimit",
        "rate limit",
        "rate-limit",
        "too many requests",
        "quota exceeded",
        "insufficient_quota",
        "429",
    )
    return any(needle in text for needle in needles)


def _build_agent_kwargs(manifest: RunnerManifest) -> dict[str, Any]:
    """Translate the manifest into kwargs for ``agents.Agent``.

    The SDK's ``Tool`` / ``Handoff`` classes are imported lazily inside
    :func:`run` so that unit tests can import this module without the
    SDK installed.  This helper stays pure-Python so it can be tested
    without the SDK at all.

    Returns:
        A dict suitable for ``Agent(**kwargs)``.
    """
    instructions = manifest.system_addendum or None
    kwargs: dict[str, Any] = {
        "name": f"bernstein-{manifest.session_id}",
        # A council role's ``model`` only ever names a council YAML file
        # path (already resolved into ``manifest.council`` by
        # ``_load_council_config`` before this is called) - it is never a
        # real model id, so it must never be handed to ``Agent(model=...)``
        # (the SDK would try to resolve the literal path string as a
        # provider/model id and fail). Leaving it ``None`` here is safe:
        # this base ``Agent`` definition is only ever used as a
        # ``.clone(model=...)`` template inside
        # ``bernstein.adapters.council_runner.run_council`` - it is never
        # itself passed to ``Runner.run``/``Runner.run_sync``.
        "model": None if manifest.council is not None else manifest.model,
    }
    if instructions:
        kwargs["instructions"] = instructions
    # Builtin tools are constructed later in ``_run_session`` (they need the
    # SDK's ``function_tool`` and the event sink), so when the manifest opts
    # into them the gateway descriptors are intentionally not attached here.
    if manifest.tool_source != "builtin" and manifest.tools:
        kwargs["tools"] = manifest.tools.copy()
    return kwargs


# Alibaba Cloud (DashScope/MaaS) OpenAI-compatible endpoints. Host-suffix
# match against ``base_url``'s hostname - covers both the public DashScope
# host (``dashscope.aliyuncs.com``) and the MaaS variant
# (``maas.aliyuncs.com``) mentioned in Alibaba's own function-calling docs.
_ALIBABA_BASE_URL_MARKERS: tuple[str, ...] = ("aliyuncs.com",)


def _is_alibaba_cloud_endpoint(base_url: str | None) -> bool:
    """Return whether *base_url* points at an Alibaba Cloud endpoint.

    Qwen 3.x models served via Alibaba Cloud (DashScope/MaaS) default to
    "thinking" mode, which makes the model reason its way out of calling
    tools and write ad-hoc scripts instead - Alibaba's own function-calling
    docs show ``extra_body={"enable_thinking": False}`` on every
    tool-calling request as the fix. Detection matches the URL's hostname
    (exact or dot-suffix) so both the public DashScope host and any
    ``*.aliyuncs.com`` MaaS variant are covered without hardcoding a
    specific hostname - and so a URL that merely CONTAINS the marker in
    its path or in an unrelated hostname (e.g. ``notaliyuncs.com``) never
    triggers the injection on another provider.
    """
    if not base_url or not isinstance(base_url, str):
        return False
    # ``urlparse`` only populates ``hostname`` when a netloc is present;
    # prefix ``//`` for scheme-less values so a bare host still parses.
    host = urlparse(base_url if "//" in base_url else f"//{base_url}").hostname or ""
    return any(host == marker or host.endswith(f".{marker}") for marker in _ALIBABA_BASE_URL_MARKERS)


def _inject_alibaba_enable_thinking(
    kwargs: dict[str, Any],
    base_url: str | None,
    *,
    context: str,
) -> dict[str, Any]:
    """Merge ``extra_body={"enable_thinking": False}`` into *kwargs* for Alibaba endpoints.

    No-op when *base_url* is not an Alibaba Cloud endpoint (see
    :func:`_is_alibaba_cloud_endpoint`) or when ``enable_thinking`` is
    already present in an existing ``extra_body`` (an explicit manifest
    value always wins over this auto-injection). Mutates and returns
    *kwargs* in place for convenient call-site chaining.

    Args:
        kwargs: The in-progress ``ModelSettings`` kwargs dict.
        base_url: The endpoint this call is bound for.
        context: Human-readable label (session id / council member) logged
            alongside the injection so it is diagnosable from the runner
            log alone (lessons.md rule 2).

    Returns:
        *kwargs*, with ``extra_body.enable_thinking`` set to ``False`` when
        applicable.
    """
    if not _is_alibaba_cloud_endpoint(base_url):
        return kwargs
    extra_body: dict[str, Any] = dict(kwargs.get("extra_body") or {})
    if "enable_thinking" in extra_body:
        logger.info(
            "%s: base_url=%r is Alibaba Cloud but extra_body.enable_thinking=%r is "
            "already set - leaving the explicit value in place",
            context,
            base_url,
            extra_body["enable_thinking"],
        )
        return kwargs
    extra_body["enable_thinking"] = False
    kwargs["extra_body"] = extra_body
    logger.info(
        "%s: base_url=%r matches Alibaba Cloud (aliyuncs.com) - injecting "
        "extra_body={'enable_thinking': False} so tool calling is reliable "
        "(Qwen 3.x reasons its way out of tool calls and writes ad-hoc "
        "scripts instead when thinking mode is left on for function-calling "
        "requests - see Alibaba's own function-calling docs)",
        context,
        base_url,
    )
    return kwargs


def _build_model_settings_kwargs(
    manifest: RunnerManifest,
    *,
    model_settings_cls: type[Any] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Translate optional sampling params into ``ModelSettings`` kwargs.

    Only fields present in the manifest are emitted so an all-``None``
    manifest yields an empty dict and the runner skips ``ModelSettings``
    entirely (exactly today's behavior).  ``top_k`` is not a first-class
    ``ModelSettings`` field in the SDK, so it travels via ``extra_args``
    for OpenAI-compatible endpoints that accept it.

    ``max_tokens`` is forwarded only when the SDK's ``ModelSettings`` exposes
    a ``max_tokens`` field. ``model_settings_cls`` is the SDK class to probe;
    when ``None`` the field is not forwarded (the SDK is not loaded, e.g. in
    unit tests), keeping the kwargs SDK-version safe.

    Args:
        base_url: The single-model endpoint this run is bound for (``None``
            for the default OpenAI endpoint, or when a council role is
            active - council members resolve their OWN per-member
            ``base_url`` in :mod:`bernstein.adapters.council_runner`
            instead, since each candidate/judge may target a different
            endpoint). When set and it is an Alibaba Cloud endpoint,
            ``extra_body={"enable_thinking": False}`` is injected - see
            :func:`_inject_alibaba_enable_thinking`.

    Returns:
        Kwargs for ``agents.ModelSettings``, possibly empty.
    """
    kwargs: dict[str, Any] = {}
    if manifest.temperature is not None:
        kwargs["temperature"] = manifest.temperature
    if manifest.top_p is not None:
        kwargs["top_p"] = manifest.top_p
    if manifest.top_k is not None:
        kwargs["extra_args"] = {"top_k": manifest.top_k}
    if manifest.max_tokens and _model_settings_accepts(model_settings_cls, "max_tokens"):
        kwargs["max_tokens"] = manifest.max_tokens
    kwargs = _inject_alibaba_enable_thinking(
        kwargs,
        base_url,
        context=f"openai_agents_runner session={manifest.session_id}",
    )
    return kwargs


def _model_settings_accepts(model_settings_cls: type[Any] | None, field_name: str) -> bool:
    """Return whether the SDK ``ModelSettings`` class exposes *field_name*.

    ``ModelSettings`` is a dataclass in the SDK, so its declared fields are
    the accepted constructor kwargs. When the class is unavailable or is not
    a dataclass the answer is ``False`` so the runner never passes a kwarg the
    installed SDK would reject.
    """
    if model_settings_cls is None:
        return False
    fields = getattr(model_settings_cls, "__dataclass_fields__", None)
    return isinstance(fields, dict) and field_name in fields


def _resolve_client_kwargs(manifest: RunnerManifest) -> dict[str, Any]:
    """Build ``AsyncOpenAI(...)`` kwargs for the optional endpoint override.

    Returns an empty dict when neither ``base_url`` nor ``api_key_env`` is
    set, in which case the runner leaves the SDK's default client alone.
    The key is resolved from the environment by NAME - the manifest never
    carries a literal secret.

    Raises:
        RuntimeError: ``api_key_env`` is set but the name fails
            :func:`validate_api_key_env_name`, or the named environment
            variable is missing or empty.
    """
    kwargs: dict[str, Any] = {}
    if manifest.base_url:
        kwargs["base_url"] = manifest.base_url
    if manifest.api_key_env:
        validate_api_key_env_name(manifest.api_key_env)
        api_key = os.environ.get(manifest.api_key_env)
        if not api_key:
            msg = (
                f"manifest api_key_env names environment variable "
                f"{manifest.api_key_env!r} but it is not set. Export "
                f"{manifest.api_key_env} before spawning the openai_agents "
                f"runner."
            )
            raise RuntimeError(msg)
        kwargs["api_key"] = api_key
    return kwargs


def _resolve_council_member_client_kwargs(member: Mapping[str, Any]) -> dict[str, Any]:
    """Build ``AsyncOpenAI(...)`` kwargs for one council candidate/judge member.

    Mirrors :func:`_resolve_client_kwargs` but reads ``base_url``/
    ``api_key_env`` from a single council member dict (one entry of
    ``manifest.council["candidates"]`` or ``manifest.council["judge"]``)
    instead of the top-level manifest fields, since each council member
    may target a different endpoint/credential.

    Raises:
        RuntimeError: ``api_key_env`` is set but fails
            :func:`validate_api_key_env_name`, or the named environment
            variable is missing or empty.
    """
    kwargs: dict[str, Any] = {}
    base_url = member.get("base_url")
    if base_url:
        kwargs["base_url"] = base_url
    api_key_env = member.get("api_key_env")
    if api_key_env:
        validate_api_key_env_name(api_key_env)
        api_key = os.environ.get(api_key_env)
        if not api_key:
            msg = (
                f"council member {member.get('model')!r} names api_key_env "
                f"{api_key_env!r} but it is not set. Export {api_key_env} before "
                f"spawning the openai_agents runner."
            )
            raise RuntimeError(msg)
        kwargs["api_key"] = api_key
    return kwargs


def _resolve_council_config_path(manifest: RunnerManifest, council_path: str) -> Path:
    """Resolve a council YAML file reference relative to ``.bernstein/`` in the workdir.

    Args:
        manifest: The parsed manifest (only ``workdir`` is used).
        council_path: The raw ``.yaml``/``.yml`` string from
            ``manifest.model`` (e.g. ``"councils/planning.yaml"``).

    Returns:
        Absolute path: ``council_path`` unchanged if already absolute,
        otherwise ``<workdir>/.bernstein/<council_path>``.
    """
    candidate = Path(council_path)
    if candidate.is_absolute():
        return candidate
    return Path(manifest.workdir) / ".bernstein" / candidate


def _load_council_config(manifest: RunnerManifest) -> dict[str, Any] | None:
    """Resolve the effective council config for this run, if any.

    Two ways a run ends up with a council config:

    1. ``manifest.council`` is already populated (e.g. a hand-written
       manifest for a direct invocation/test) - returned unchanged,
       ``manifest.model`` is not inspected.
    2. ``manifest.model`` names a ``.yaml``/``.yml`` file (the
       ``role_model_policy.<role>.model: "councils/foo.yaml"`` convention -
       see :class:`RunnerManifest`'s ``council`` field docstring). The file
       is resolved relative to ``.bernstein/`` in ``manifest.workdir`` (see
       :func:`_resolve_council_config_path`), parsed as YAML, and validated
       to have a non-empty ``candidates`` list and a ``judge`` mapping -
       the same shape :func:`bernstein.adapters.council_runner.run_council`
       expects (mirrors
       :class:`bernstein.core.config.config_schema.CouncilConfig`).

    Returns:
        The parsed council config dict, or ``None`` when neither applies -
        the ordinary single-model path (unchanged behavior).

    Raises:
        RuntimeError: ``manifest.model`` ends in ``.yaml``/``.yml`` but the
            referenced file does not exist, is not valid YAML, is not a
            mapping, or is missing a non-empty ``candidates`` list / a
            ``judge`` mapping. Surfaced to :func:`run`'s existing
            ``config_invalid`` error path exactly like any other
            manifest-time misconfiguration.
    """
    if manifest.council is not None:
        logger.info(
            "openai_agents_runner session=%s: manifest.council already populated - "
            "using it as-is (manifest.model=%r is not inspected for a .yaml suffix)",
            manifest.session_id,
            manifest.model,
        )
        return manifest.council

    if not manifest.model.endswith((".yaml", ".yml")):
        return None

    config_path = _resolve_council_config_path(manifest, manifest.model)
    logger.info(
        "openai_agents_runner session=%s: manifest.model=%r ends in .yaml/.yml - "
        "loading it as a council definition file from %s",
        manifest.session_id,
        manifest.model,
        config_path,
    )
    if not config_path.exists():
        msg = (
            f"manifest.model {manifest.model!r} looks like a council definition file "
            f"(ends in .yaml/.yml) but {config_path} does not exist"
        )
        raise RuntimeError(msg)

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        parsed: object = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        msg = f"failed to read/parse council file {config_path}: {type(exc).__name__}: {exc}"
        raise RuntimeError(msg) from exc

    if not isinstance(parsed, dict):
        msg = f"council file {config_path} must be a YAML mapping, got {type(parsed).__name__}"
        raise RuntimeError(msg)

    raw_candidates = parsed.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        msg = f"council file {config_path}: 'candidates' must be a non-empty list"
        raise RuntimeError(msg)
    raw_judge = parsed.get("judge")
    if not isinstance(raw_judge, dict):
        msg = f"council file {config_path}: 'judge' is required and must be a mapping"
        raise RuntimeError(msg)

    logger.info(
        "openai_agents_runner session=%s: council file %s parsed OK: %d candidates, judge model=%r, timeout=%r",
        manifest.session_id,
        config_path,
        len(raw_candidates),
        raw_judge.get("model"),
        parsed.get("timeout"),
    )
    return cast("dict[str, Any]", parsed)


def _resolve_tokens_sidecar_path(manifest: RunnerManifest) -> Path:
    """Return the ``.tokens`` sidecar path the orchestrator actually reads.

    Bug 13 (2026-07-02): the orchestrator's live cost-metering loop
    (``Orchestrator._record_live_costs``) only prices a session once
    ``AgentSession.tokens_used`` is non-zero, and that field is populated
    exclusively by ``TokenGrowthMonitor.read_tokens()`` tailing
    ``<orchestrator-root>/.sdd/runtime/<session_id>.tokens`` - the same
    sidecar Claude Code's wrapper script writes (see
    :mod:`bernstein.adapters.claude_wrapper_script`). Before this fix the
    openai_agents runner emitted a ``usage`` event to its own stdout/log
    only, which nothing tails for cost purposes, so
    ``AgentSession.tokens_used`` stayed ``0`` for the entire run and the
    live-cost loop skipped the session on every tick - the direct cause of
    the observed ``spent_usd: 0.0`` / empty ``usages`` run.

    Mirrors :func:`_resolve_heartbeat_dir`'s resolution logic: ``workdir``
    is a per-session worktree under default isolation, so the sidecar must
    live under the orchestrator-root ``.sdd/runtime/`` directory (the
    heartbeat directory's parent), not a worktree-relative path, or the
    monitor polling the orchestrator root would never see it.
    """
    if manifest.heartbeat_dir:
        runtime_dir = Path(manifest.heartbeat_dir).parent
    else:
        runtime_dir = Path(manifest.workdir) / ".sdd" / "runtime"
    return runtime_dir / f"{manifest.session_id}.tokens"


def _append_tokens_sidecar(path: Path, input_tokens: int, output_tokens: int) -> None:
    """Append one usage record to the ``.tokens`` sidecar file.

    Uses the exact schema Claude Code's wrapper script writes
    (``{"ts": float, "in": int, "out": int}``) so the orchestrator's
    generic ``TokenGrowthMonitor.read_tokens()`` - which just tails
    whatever provider wrote the file - works unchanged for the
    openai_agents path. Best-effort: a sidecar write failure must never
    fail the run, only lose live-cost visibility for this call (logged at
    WARNING so the loss itself is diagnosable).

    Args:
        path: Absolute sidecar path from :func:`_resolve_tokens_sidecar_path`.
        input_tokens: Prompt tokens for this call.
        output_tokens: Completion tokens for this call.
    """
    if input_tokens <= 0 and output_tokens <= 0:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps({"ts": time.time(), "in": input_tokens, "out": output_tokens})
        with path.open("a", encoding="utf-8") as fh:
            fh.write(record + "\n")
    except OSError as exc:
        # "tokens" here is the usage-count sidecar file, not a credential.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.warning("failed to write tokens sidecar %s: %s", path, exc)


def _extract_usage_tokens(result: Any) -> tuple[int, int, int]:
    """Extract ``(input_tokens, output_tokens, tool_calls)`` from a result-like object.

    Accepts anything shaped like an SDK run result: ``result.usage`` first
    (aggregate), then the ``result.raw_responses`` per-call fallback (bug-13
    follow-up: ``RunResult`` never defines ``usage``, so the fallback is the
    path that actually fires on real SDK objects). Also accepts the
    ``run_data`` payload some SDK exceptions carry (e.g. ``MaxTurnsExceeded``
    exposes the partial run's ``raw_responses`` via ``exc.run_data``), and
    ``None`` (returns all zeros).

    Args:
        result: An SDK ``RunResult``, an exception's ``run_data`` object,
            or ``None``.

    Returns:
        ``(input_tokens, output_tokens, tool_calls)``. All zero when no
        usable usage data exists on the object.
    """
    usage: Any = getattr(result, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    tool_calls = int(getattr(usage, "tool_calls", 0) or 0)

    if input_tokens <= 0 and output_tokens <= 0:
        raw_responses: Any = getattr(result, "raw_responses", None) or []
        fallback_input = 0
        fallback_output = 0
        for raw_response in raw_responses:
            raw_usage = getattr(raw_response, "usage", None)
            fallback_input += int(getattr(raw_usage, "input_tokens", 0) or 0)
            fallback_output += int(getattr(raw_usage, "output_tokens", 0) or 0)
        if fallback_input > 0 or fallback_output > 0:
            input_tokens = fallback_input
            output_tokens = fallback_output

    return input_tokens, output_tokens, tool_calls


def _log_max_turns_exceeded(exc: BaseException, max_turns: int | None) -> None:
    """Log a WARNING with full context when a session hits the turn cap.

    Bug 13 (D2 minimax attempt-e938bd33): MaxTurnsExceeded was the dominant
    failure and surfaced only as a generic runtime error - no record of the
    configured cap, the turns actually burned, or whether the agent had
    already finished its work (backend hit the cap AFTER committing and
    POSTing /complete, so the kill wasted completed work). This makes the
    cap-hit a 2-minute diagnosis from the runner log alone.

    Args:
        exc: The ``MaxTurnsExceeded`` exception (its ``run_data`` carries the
            partial run's ``raw_responses``/``new_items`` when the SDK
            populated them).
        max_turns: The effective cap forwarded to ``Runner.run_sync``
            (``None`` = SDK default).
    """
    run_data = getattr(exc, "run_data", None)
    raw_responses = getattr(run_data, "raw_responses", None) or []
    turns_used: int | str = len(raw_responses) if raw_responses else "unknown"
    # Best-effort detection of already-completed work: the agent signals
    # completion through the run_command builtin tool, so a completion command
    # inside any tool-call item of the partial run means the work was ALREADY
    # done when the cap fired. Match both the `bernstein task complete` front
    # door agents are instructed to use and a direct POST to the endpoint,
    # which older sessions and hand-written commands still produce.
    work_completed = "unknown"
    try:
        new_items = getattr(run_data, "new_items", None) or []
        for item in new_items:
            raw_item = getattr(item, "raw_item", item)
            arguments = getattr(raw_item, "arguments", None)
            if isinstance(arguments, str) and ("/complete" in arguments or "task complete" in arguments):
                work_completed = "yes"
                break
        else:
            if new_items:
                work_completed = "no"
    except Exception:  # diagnostics only - never mask the real failure
        work_completed = "unknown"
    logger.warning(
        "MaxTurnsExceeded: session hit the turn cap "
        "(max_turns=%s, turns_used=%s, work_already_completed=%s). "
        "Raise tuning.agent.max_turns / %s / manifest max_turns if this "
        "workflow legitimately needs more turns.",
        max_turns if max_turns is not None else "SDK-default",
        turns_used,
        work_completed,
        MAX_TURNS_ENV_VAR,
    )


def _emit_session_usage(
    manifest: RunnerManifest,
    usage_source: Any,
    *,
    source_desc: str,
    model: str | None = None,
) -> None:
    """Extract, price, emit, and sidecar the session's token usage.

    Single choke point for usage accounting, callable from BOTH the success
    path (``usage_source`` = the SDK ``RunResult``) and the exception path
    (``usage_source`` = the exception's ``run_data`` payload, or ``None``).

    A task-level council run (``manifest.council is not None``) calls this
    once PER council member (each live candidate, plus the judge) instead
    of once for the whole session, passing that member's own ``result``
    object as ``usage_source`` and its real model id as ``model`` - see
    ``bernstein.adapters.council_runner.CouncilRunResult.member_usage``.
    Without the ``model`` override every council member would be priced
    under ``manifest.model``, which for a council role is the ``.yaml``
    config file path, not a real model id - that path isn't in the pricing
    table, so ``price_model_usage`` fell back to $0 with a warning for
    every council run. Passing each member's real model id here is the fix.

    D2 MiniMax attempt-3 (2026-07-03) proof: the usage-extraction block
    previously lived only after the ``try/except`` around
    ``Runner.run_sync``, so ANY exception - including ``MaxTurnsExceeded``,
    a routine condition where the agent has already made up to ``max_turns``
    real, billable LLM calls - skipped it entirely. Six backend agents each
    burned 10 real MiniMax turns and emitted zero ``usage`` events; the run's
    cost file read ``spent_usd: 0.0, usages: []`` despite real spend. This
    helper makes the extraction reachable from the exception handler: real
    partial usage is priced and sidecar'd exactly like a successful run's,
    and when genuinely no data exists a ``usage_missing`` event still fires
    so downstream sees "unknown", never a silent zero.

    Args:
        manifest: The runner manifest (model, session id, sidecar path).
        usage_source: Object to extract usage from - an SDK ``RunResult``,
            an exception's ``run_data``, or ``None``.
        source_desc: Human-readable description of where ``usage_source``
            came from (e.g. ``"result"``, ``"MaxTurnsExceeded.run_data"``),
            logged so a $0 run names the exact path that produced it.
        model: The real model id to price/report usage under. Defaults to
            ``manifest.model`` - pass this explicitly for a council member
            (its real candidate/judge model id), since ``manifest.model``
            for a council role names the ``.yaml`` config file, not a
            priceable model.
    """
    model_id = model if model is not None else manifest.model
    input_tokens, output_tokens, tool_calls = _extract_usage_tokens(usage_source)

    if input_tokens <= 0 and output_tokens <= 0:
        # No usable token counts. Never fabricate tokens/cost and never
        # write the sidecar with zeros - both would poison the
        # orchestrator's live-cost accounting with fake data. Instead, emit
        # a usage event flagged ``usage_missing`` (so downstream consumers
        # can tell "zero spend" apart from "we don't know") and log loudly,
        # naming the model and source, so a $0.00 run is a 2-minute
        # diagnosis instead of a silent no-op.
        logger.warning(
            "llm_call session=%s model=%s source=%s: SDK returned no usage "
            "data (usage is None/empty and raw_responses carried no usable "
            "usage) - cost metering for this call is unavailable, not zero",
            manifest.session_id,
            model_id,
            source_desc,
        )
        emit_event(
            {
                "type": "usage",
                "input_tokens": 0,
                "output_tokens": 0,
                "tool_calls": tool_calls,
                "model": model_id,
                "usage_missing": True,
                "usage_source": source_desc,
            },
        )
        return

    # Bug 13 fix: price the call (visible $0 + WARNING for unpriced models,
    # never a silent drop - see price_model_usage docstring), log the
    # per-call INFO line that would have made the original $0.00 run a
    # 2-minute diagnosis, and write the .tokens sidecar so the
    # orchestrator's live cost-metering loop actually sees this session
    # (see _resolve_tokens_sidecar_path docstring for why the previous
    # stdout-only "usage" event never reached it).
    from bernstein.core.cost.model_prices import price_model_usage

    price_result = price_model_usage(model_id, input_tokens, output_tokens)
    # ``Runner.run_sync`` aggregates every internal turn into one cumulative
    # usage total (whether from ``result.usage``, the ``raw_responses``
    # fallback sum, or an exception's partial ``run_data``), so this call's
    # cost *is* the session's running total to this point - there is no
    # separate accumulator to maintain across multiple SDK calls in-process.
    running_total_usd = price_result.cost_usd
    logger.info(
        "llm_call session=%s model=%s source=%s input_tokens=%d "
        "output_tokens=%d cost_usd=%.6f priced=%s running_total_usd=%.6f",
        manifest.session_id,
        model_id,
        source_desc,
        input_tokens,
        output_tokens,
        price_result.cost_usd,
        price_result.priced,
        running_total_usd,
    )

    emit_event(
        {
            "type": "usage",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tool_calls": tool_calls,
            "model": model_id,
            "cost_usd": price_result.cost_usd,
            "priced": price_result.priced,
            "running_total_usd": running_total_usd,
            "usage_source": source_desc,
        },
    )

    _append_tokens_sidecar(
        _resolve_tokens_sidecar_path(manifest),
        input_tokens,
        output_tokens,
    )


def _redacted_keys(mapping: Any) -> Any:
    """Redact a header/body mapping for logging: return only its sorted key
    names, never the values.

    ``extra_headers``/``extra_body`` are where provider auth
    (``Authorization: Bearer <key>``, ``X-Api-Key``, OpenRouter keys) is
    conventionally placed, so their values must never reach the logs. Returns
    the sorted list of key names for a mapping, ``None`` for ``None`` (nothing
    configured), and a redacted marker for any non-mapping value so a secret
    can never be logged verbatim.
    """
    if mapping is None:
        return None
    if isinstance(mapping, Mapping):
        return sorted(str(k) for k in mapping)
    return "<redacted: non-mapping value>"


def _deepseek_debug_tool_schema_summary(tools: Any) -> list[dict[str, Any]]:
    """Best-effort summary of a tool list's name + strict-schema flag.

    [DEEPSEEK-DEBUG] diagnostic helper (2026-07-03): the empty-completion /
    malformed-tool-call bug on ``deepseek/deepseek-chat`` via OpenRouter is
    hypothesized to be triggered by the OpenAI Agents SDK's default
    ``strict_json_schema=True`` on ``FunctionTool`` (see ``agents/tool.py``
    ``strict_json_schema: bool = True`` and ``function_tool(strict_mode=True)``
    defaults, and ``Converter.tool_to_openai`` which puts
    ``"strict": tool.strict_json_schema`` directly into the outbound
    ``ChatCompletionToolParam``). This walks whatever tool objects/dicts are
    about to be handed to the SDK and reports, per tool, its name and
    whatever strict-schema signal is discoverable - without assuming a
    specific SDK version's exact attribute layout (best-effort; never
    raises).

    Args:
        tools: The tool list about to be attached to ``agent_kwargs["tools"]``
            - either raw dicts (gateway tool_source) or SDK ``Tool``
            instances (builtin tool_source, built via ``@function_tool``).

    Returns:
        List of ``{"name": ..., "strict_json_schema": ..., "kind": ...,
        "params_json_schema": ...}`` summaries. Never raises.
    """
    summaries: list[dict[str, Any]] = []
    try:
        for tool in tools or []:
            entry: dict[str, Any] = {"kind": type(tool).__name__}
            if isinstance(tool, dict):
                entry["name"] = tool.get("name", "<unnamed-dict-tool>")
                entry["strict_json_schema"] = tool.get("strict")
                params = tool.get("parameters") or tool.get("params_json_schema")
                entry["params_json_schema"] = params
            else:
                entry["name"] = getattr(tool, "name", "<unnamed-tool-object>")
                entry["strict_json_schema"] = getattr(tool, "strict_json_schema", "<attr-absent>")
                params_schema = getattr(tool, "params_json_schema", None)
                entry["params_json_schema"] = params_schema
            summaries.append(entry)
    except Exception as exc:  # diagnostics only - never mask the real failure
        summaries.append({"error": f"tool schema summary failed: {type(exc).__name__}: {exc}"})
    return summaries


def _resolve_heartbeat_dir(manifest: RunnerManifest) -> Path:
    """Return the directory heartbeat files must be written to.

    Prefers ``manifest.heartbeat_dir`` (the orchestrator-root directory
    the ``HeartbeatMonitor`` polls, injected by the spawner) and falls
    back to a workdir-relative path for standalone runner invocations.
    """
    if manifest.heartbeat_dir:
        return Path(manifest.heartbeat_dir)
    return Path(manifest.workdir) / ".sdd" / "runtime" / "heartbeats"


def _start_heartbeat(
    session_id: str,
    heartbeat_dir: Path,
    interval_s: float = 15.0,
) -> threading.Event:
    """Write heartbeat files while the runner process is alive.

    Mirrors the payload schema of ``_start_heartbeat_proxy`` in
    :mod:`bernstein.core.agents.spawner_sandbox_session` so the
    orchestrator's ``HeartbeatMonitor`` treats runner sessions exactly
    like sandbox sessions.

    Args:
        session_id: Runner session identifier (heartbeat file basename).
        heartbeat_dir: Directory the heartbeat file is written to.  Must
            be the same ``.sdd/runtime/heartbeats`` directory the
            orchestrator's ``HeartbeatMonitor`` reads - see
            :func:`_resolve_heartbeat_dir`.
        interval_s: Seconds between heartbeat writes (default 15).

    Returns:
        A :class:`threading.Event` the caller sets to stop the writer.
    """
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        with contextlib.suppress(OSError):  # best effort
            heartbeat_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_file = heartbeat_dir / f"{session_id}.json"
        while not stop_event.is_set():
            payload = json.dumps(
                {
                    "timestamp": int(time.time()),
                    "phase": "implementing",
                    "progress_pct": 0,
                    "current_file": "",
                    "message": "openai-agents runner working",
                    "status": "working",
                    "files_changed": 0,
                }
            )
            with contextlib.suppress(OSError):  # best effort
                heartbeat_file.write_text(payload, encoding="utf-8")
            stop_event.wait(interval_s)

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"heartbeat-runner-{session_id}",
        daemon=True,
    )
    thread.start()
    return stop_event


def run(manifest: RunnerManifest) -> int:
    """Execute the SDK session described by ``manifest``.

    Emits structured events to stdout throughout the run.  Returns an
    integer exit code suitable for ``sys.exit``.

    Args:
        manifest: Parsed manifest describing the run.

    Returns:
        Process exit code.  See module docstring for the contract.
    """
    # Wave 3 (per-agent instrumentation): stand up this process's
    # RunInstrumenter before any SDK/event work so every subsequent
    # emit_event() call (including the "start" event right below) is
    # eligible for mirroring. run_id comes from the orchestrator via the
    # BERNSTEIN_RUN_ID env var (threaded through env_isolation's
    # allowlist); task_id from the manifest (spawner_core-injected, see
    # spawner_core.py's openai_agents-scoped mcp_config injection);
    # agent_id is this session's own id. Falls back to literal "unknown"
    # values rather than failing the run when either is absent (e.g. a
    # hand-written manifest for a direct-invocation test).
    _reset_instrumentation_counters()
    run_id = os.environ.get("BERNSTEIN_RUN_ID", "unknown")
    task_id = manifest.task_id or "unknown"
    # Prefer ``instrumentation_root`` (the orchestrator's project root,
    # injected by spawner_core) over ``manifest.workdir``. Under default
    # worktree isolation ``workdir`` is a per-session worktree that gets
    # deleted on cleanup/merge - instrumentation written there would either
    # never be found (wave-2's summary.json lives at the project root) or
    # vanish entirely once the worktree is torn down. Hand-written
    # manifests (tests, direct invocation) have no worktree at all, so
    # falling back to ``workdir`` there is correct.
    instrumentation_base = manifest.instrumentation_root or manifest.workdir
    agent_dir = resolve_agent_dir(Path(instrumentation_base), run_id, task_id, manifest.session_id)

    # Bug fix (instrumentation audit, bug 3): if this agent process is
    # working a BATCH of tasks (manifest.task_ids has more than one entry),
    # resolve an agent dir for every OTHER task in the batch too and pass
    # them as extra_dirs so init_instrumenter fans every JSONL write out to
    # all of them - see RunnerManifest.task_ids and RunInstrumenter.extra_dirs
    # docstrings for the full root-cause writeup.
    batch_task_ids = list(manifest.task_ids or [])
    extra_dirs = [
        resolve_agent_dir(Path(instrumentation_base), run_id, other_task_id, manifest.session_id)
        for other_task_id in batch_task_ids
        if other_task_id and other_task_id != task_id
    ]
    if batch_task_ids:
        logger.info(
            "Batched-task instrumentation check: manifest.task_ids=%s primary task_id=%s -> "
            "%d extra instrumentation dir(s) will receive a full copy of this agent's records",
            batch_task_ids,
            task_id,
            len(extra_dirs),
        )
    if not manifest.task_id:
        logger.debug(
            "run(): manifest.task_id is unset - this session's instrumentation will be bucketed "
            "under the literal 'unknown' task_id; run_id from env BERNSTEIN_RUN_ID=%s",
            os.environ.get("BERNSTEIN_RUN_ID"),
        )
    init_instrumenter(
        run_id=run_id, task_id=task_id, agent_id=manifest.session_id, base_dir=agent_dir, extra_dirs=extra_dirs
    )
    logger.info(
        "Instrumentation base dir resolved: instrumentation_root=%r workdir=%r -> using %r -> agent_dir=%s "
        "extra_dirs=%s",
        manifest.instrumentation_root,
        manifest.workdir,
        instrumentation_base,
        agent_dir,
        extra_dirs,
    )

    # Every effective sampling/endpoint param is logged here.  The key
    # itself is never logged - only the NAME of the env var that holds it.
    emit_event(
        {
            "type": "start",
            "session_id": manifest.session_id,
            "model": manifest.model,
            "sandbox_provider": manifest.sandbox_provider,
            "temperature": manifest.temperature,
            "top_p": manifest.top_p,
            "top_k": manifest.top_k,
            "base_url": manifest.base_url,
            "api_key_env": manifest.api_key_env,
        },
    )

    # Resolve a task-level council config BEFORE any other SDK work, so a
    # malformed council YAML file (missing/unreadable/missing
    # candidates-or-judge) fails loudly at startup instead of mid-session.
    # When ``manifest.model`` names a council file this REPLACES ``manifest``
    # with a copy carrying ``council`` populated - every downstream read of
    # ``manifest.council`` (here and in ``_run_session``) sees the resolved
    # config from this point on.
    try:
        council_cfg = _load_council_config(manifest)
    except RuntimeError as exc:
        emit_event(
            {
                "type": "error",
                "kind": "config_invalid",
                "message": str(exc),
            },
        )
        return EXIT_MANIFEST_ERROR
    if council_cfg is not None and manifest.council is None:
        manifest = dataclasses.replace(manifest, council=council_cfg)

    # Resolve the endpoint override before any SDK work so a missing key
    # env var fails loudly at startup instead of mid-session.
    try:
        client_kwargs = _resolve_client_kwargs(manifest)
    except RuntimeError as exc:
        emit_event(
            {
                "type": "error",
                "kind": "config_invalid",
                "message": str(exc),
            },
        )
        return EXIT_MANIFEST_ERROR

    heartbeat_stop = _start_heartbeat(manifest.session_id, _resolve_heartbeat_dir(manifest))
    try:
        return _run_session(manifest, client_kwargs)
    finally:
        heartbeat_stop.set()


def _run_session(manifest: RunnerManifest, client_kwargs: dict[str, Any]) -> int:
    """Run the SDK session after startup validation has passed.

    Args:
        manifest: Parsed manifest describing the run.
        client_kwargs: Non-empty when the manifest overrides the endpoint
            or API key; forwarded to ``AsyncOpenAI``.

    Returns:
        Process exit code.  See module docstring for the contract.
    """
    try:
        # Lazy import so the module itself stays importable without
        # the optional ``openai-agents`` package.  Tests stub this by
        # patching ``bernstein.adapters.openai_agents_runner.run``.
        import agents as agents_sdk  # type: ignore[import-not-found]
    except ImportError:
        emit_event(
            {
                "type": "error",
                "kind": "sdk_missing",
                "message": _sdk_missing_message(),
            },
        )
        return EXIT_SDK_MISSING

    # Cast the SDK module to ``Any`` so strict Pyright does not need type
    # stubs for the optional dependency.  The cast is safe because every
    # attribute access is guarded by the ``AttributeError`` handler below.
    sdk = cast("Any", agents_sdk)
    try:
        agent_cls: Any = sdk.Agent
        runner_cls: Any = sdk.Runner
    except AttributeError as exc:
        emit_event(
            {
                "type": "error",
                "kind": "sdk_incompatible",
                "message": f"openai-agents SDK is missing expected symbols: {exc}",
            },
        )
        return EXIT_GENERIC

    # Set when ``manifest.base_url`` names a custom OpenAI-compatible
    # endpoint: holds the ``AsyncOpenAI`` client the Agent's model must be
    # built against explicitly (see the ``explicit_model_client`` usage
    # below ``_build_agent_kwargs``).
    explicit_model_client: Any = None
    if client_kwargs and manifest.council is None:
        # Skipped when ``manifest.council`` is set: a council role builds
        # one dedicated ``AsyncOpenAI`` client PER council member (see
        # ``bernstein.adapters.council_runner.run_council``), so the single
        # top-level client/default-client dance here does not apply - each
        # member resolves its own endpoint independently of the role's
        # top-level ``base_url``/``api_key_env``.
        #
        # The manifest overrides the endpoint and/or API key.  Hand the SDK
        # a dedicated client instead of letting it read the ambient
        # OPENAI_* environment.
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]

            client = AsyncOpenAI(**client_kwargs)
            if manifest.base_url:
                # Third-party OpenAI-compatible endpoints (the reason
                # ``base_url`` exists) serve the chat-completions API,
                # not ``/responses``, so switch the SDK's default API.
                # ``use_for_tracing=False`` keeps the SDK's tracing
                # exporter from uploading traces to api.openai.com
                # authenticated with the third-party key.
                sdk.set_default_openai_client(client, use_for_tracing=False)
                sdk.set_default_openai_api("chat_completions")
                # D1 openrouter fix (2026-07-02): setting the default
                # client is NOT enough on its own. ``Agent(model=<str>)``
                # is resolved by ``RunConfig.model_provider`` (a fresh
                # ``agents.MultiProvider()`` by default -
                # ``agents/run_config.py:220``) via
                # ``turn_preparation.get_model()``
                # (``agents/run_internal/turn_preparation.py:126-135``),
                # which for a plain string falls through to
                # ``model_provider.get_model(agent.model)``. ``MultiProvider``
                # unconditionally splits the model string on the first "/"
                # to find a provider prefix
                # (``agents/models/multi_provider.py:154-161``,
                # ``_get_prefix_and_model_name``) and raises
                # ``UserError(f"Unknown prefix: {prefix}")`` for anything
                # that isn't "openai"/"litellm"/"any-llm"/an explicit
                # provider-map entry (``multi_provider.py:173`` and
                # ``:221``). OpenRouter model ids legitimately contain a
                # "vendor/model" slash (e.g. "deepseek/deepseek-chat"), so
                # the SDK misreads "deepseek" as an SDK provider prefix and
                # aborts before any tool call. Building the ``Model``
                # instance ourselves sidesteps this entirely:
                # ``get_model()`` returns ``agent.model`` directly, with no
                # prefix parsing, whenever it is already an
                # ``agents.Model`` instance rather than a string
                # (``turn_preparation.py:132-133``).
                explicit_model_client = client
            else:
                sdk.set_default_openai_client(client)
        except Exception as exc:
            emit_event(
                {
                    "type": "error",
                    "kind": "runtime",
                    "message": f"failed to configure OpenAI client: {type(exc).__name__}: {exc}",
                },
            )
            return EXIT_GENERIC

    # Resolved before the try so the except handler can report the effective
    # cap even when the exception fires before/inside ``Runner.run_sync``.
    max_turns: int | None = None
    try:
        agent_kwargs = _build_agent_kwargs(manifest)
        if manifest.council is not None:
            # TASK-LEVEL council override: this role's ENTIRE task run is
            # driven by N candidate models running the whole task
            # independently in parallel, then a judge model synthesizes one
            # improved answer from their outputs - see
            # ``bernstein.adapters.council_runner.run_council``, called
            # below instead of ``Runner.run_sync``. ``_build_agent_kwargs``
            # already left this base agent's ``model`` unset (``None``) for
            # a council role (manifest.model only ever names the council
            # YAML file, never a real model id) - ``run_council`` builds
            # each candidate/judge's own model via
            # ``agent.clone(model=...)`` off this same base definition, so
            # no model needs to be set on ``agent_kwargs`` here.
            logger.info(
                "openai_agents_runner session=%s: council enabled for this role (%d "
                "candidates configured) - manifest.model=%r names the council file, "
                "not a real model id",
                manifest.session_id,
                len(manifest.council.get("candidates", [])),
                manifest.model,
            )
        elif explicit_model_client is not None:
            # Never log the client itself (it carries the API key) - only
            # the model id and base_url, both non-secret.
            logger.info(
                "openai_agents_runner session=%s: routing model=%r through an "
                "explicit OpenAIChatCompletionsModel bound to base_url=%r "
                "instead of a bare model string, so the SDK's MultiProvider "
                "never prefix-parses a vendor/model id from a custom endpoint",
                manifest.session_id,
                manifest.model,
                manifest.base_url,
            )
            agent_kwargs["model"] = sdk.OpenAIChatCompletionsModel(
                model=manifest.model,
                openai_client=explicit_model_client,
            )
        if manifest.tool_source == "builtin":
            # Opt-in workdir-sandboxed builtins for runs with no MCP gateway
            # reachable. Every call is recorded to this same event stream via
            # ``emit_event`` so the run stays auditable and replayable.
            from bernstein.adapters.openai_agents_builtins import (
                build_builtin_tools,
                selected_builtin_names,
            )

            active_names = selected_builtin_names(
                manifest.sandbox_provider,
                allow_run_command=manifest.allow_run_command,
            )
            emit_event(
                {
                    "type": "progress",
                    "message": f"builtin tools active: {', '.join(active_names)}",
                    "tool_source": "builtin",
                },
            )
            agent_kwargs["tools"] = build_builtin_tools(
                Path(manifest.workdir),
                emit_event,
                sandbox_provider=manifest.sandbox_provider,
                allow_run_command=manifest.allow_run_command,
            )

            # Disable strict JSON schema for non-OpenAI models (e.g.
            # deepseek-chat via OpenRouter).  The SDK defaults
            # strict_json_schema=True on every FunctionTool, which sends
            # {"strict": true} in the tool schema.  Models that don't
            # support OpenAI's strict structured-output mode return empty
            # responses or malformed tool calls when they see this flag.
            _model_lower = manifest.model.lower()
            _is_openai_native = any(_model_lower.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-", "chatgpt-"))
            if not _is_openai_native:
                _relaxed_count = 0
                for tool in agent_kwargs.get("tools", []):
                    if getattr(tool, "strict_json_schema", None) is True:
                        tool.strict_json_schema = False
                        _relaxed_count += 1
                if _relaxed_count:
                    logger.info(
                        "[DEEPSEEK-DEBUG] Relaxed strict_json_schema=False on %d tools "
                        "for non-OpenAI model %r (strict mode causes empty responses "
                        "on deepseek-chat and other non-OpenAI models via OpenRouter)",
                        _relaxed_count,
                        manifest.model,
                    )

        # Council roles resolve their own per-member base_url independently
        # (see council_runner._run_candidate/_run_judge) since each
        # candidate/judge may target a different endpoint - the top-level
        # manifest.base_url must not be applied to the shared base agent in
        # that case, or every council member would inherit the enable_thinking
        # override intended for only one of them.
        settings_base_url = None if manifest.council is not None else manifest.base_url
        settings_kwargs = _build_model_settings_kwargs(
            manifest,
            model_settings_cls=sdk.ModelSettings,
            base_url=settings_base_url,
        )
        if settings_kwargs:
            agent_kwargs["model_settings"] = sdk.ModelSettings(**settings_kwargs)
        _log_initial_conversation_messages(manifest)
        agent: Any = agent_cls(**agent_kwargs)
        # No ``run_config`` is passed: the SDK's ``run_config`` kwarg must
        # be a real ``agents.RunConfig`` instance - a plain dict crashes
        # inside ``run.py`` on ``run_config.session_input_callback`` - and
        # none of the manifest fields the old dict carried
        # (sandbox_provider, workdir, timeout_seconds, mcp_servers) are
        # ``RunConfig`` fields anyway. Every per-run knob we need (model,
        # endpoint client, sampling settings) already rides on the Agent
        # via ``_build_agent_kwargs``.
        run_sync_kwargs: dict[str, Any] = {}
        max_turns = _resolve_max_turns(manifest.max_turns)
        if max_turns is not None:
            run_sync_kwargs["max_turns"] = max_turns
        if manifest.council is None:
            # Wave 4 (per-turn instrumentation): only wired for the single-model
            # Runner.run_sync path below - run_sync_kwargs is NOT forwarded into
            # run_council (see the comment on the max_turns line above; a council
            # role runs N whole-task candidates via its own internal Runner calls
            # in bernstein.adapters.council_runner, which is a separate, currently
            # un-instrumented-at-this-granularity code path - documented gap, see
            # this function's docstring / wave-3 final report for the equivalent
            # council caveat on usage accounting).
            hooks_instance = _build_instrumentation_hooks(sdk, manifest)
            run_sync_kwargs["hooks"] = hooks_instance
            logger.debug(
                "hook registration: passing hooks=%s to Runner.run_sync session=%s",
                type(hooks_instance).__name__,
                manifest.session_id,
            )
        if manifest.council is not None:
            # Task-level council: run N candidates through the WHOLE task
            # in parallel, then a judge synthesizes one answer from their
            # outputs. Imported lazily (not at module top level) to match
            # this module's existing lazy-SDK-adjacent-import convention
            # for optional adapter subsystems (see the
            # ``bernstein.adapters.openai_agents_builtins`` import elsewhere
            # in this function). ``run_council`` resolves its own
            # ``max_turns`` internally (mirrors this same
            # ``_resolve_max_turns`` call), so it is not forwarded here.
            from bernstein.adapters.council_runner import run_council

            logger.info(
                "openai_agents_runner session=%s: running task-level council instead of "
                "a single Runner.run_sync call (%d candidates configured)",
                manifest.session_id,
                len(manifest.council.get("candidates", [])),
            )
            result: Any = run_council(agent, manifest.prompt, manifest.council, manifest)
        else:
            # [DEEPSEEK-DEBUG] Unconditional (not gated behind a debug flag)
            # pre-call diagnostic for the deepseek/deepseek-chat-via-OpenRouter
            # empty-completion / malformed-tool-call investigation (2026-07-03).
            # Logged at INFO so it is captured on every run without operator
            # opt-in. Never logs the API key itself - only the env var NAME.
            _model_settings_obj = agent_kwargs.get("model_settings")
            _model_settings_repr = (
                {
                    "temperature": getattr(_model_settings_obj, "temperature", None),
                    "top_p": getattr(_model_settings_obj, "top_p", None),
                    "tool_choice": getattr(_model_settings_obj, "tool_choice", None),
                    "parallel_tool_calls": getattr(_model_settings_obj, "parallel_tool_calls", None),
                    "max_tokens": getattr(_model_settings_obj, "max_tokens", None),
                    "extra_args": getattr(_model_settings_obj, "extra_args", None),
                    # extra_headers/extra_body are the SDK-conventional home for
                    # provider auth (Authorization/api-key). Log only the KEY NAMES,
                    # never the values, so this diagnostic can never leak a secret
                    # if an auth header is ever configured here.
                    "extra_headers_keys": _redacted_keys(getattr(_model_settings_obj, "extra_headers", None)),
                    "extra_body_keys": _redacted_keys(getattr(_model_settings_obj, "extra_body", None)),
                }
                if _model_settings_obj is not None
                else "<no ModelSettings constructed - settings_kwargs was empty>"
            )
            _tool_list_for_log = agent_kwargs.get("tools", [])
            # Only the NAME of the credential environment variable is logged here,
            # never the secret value it resolves to.
            _key_env_name_for_log = manifest.api_key_env
            logger.info(
                "[DEEPSEEK-DEBUG] pre-call session=%s model=%r base_url=%r credential env var=%r "
                "explicit_model_client=%s tool_source=%r tool_count=%d max_turns=%s",
                manifest.session_id,
                manifest.model,
                manifest.base_url,
                _key_env_name_for_log,
                explicit_model_client is not None,
                manifest.tool_source,
                len(_tool_list_for_log),
                max_turns,
            )
            logger.info(
                "[DEEPSEEK-DEBUG] pre-call session=%s model_settings=%s "
                "(tool_choice/parallel_tool_calls of None means the SDK/provider "
                "default applies - bernstein never sets these explicitly)",
                manifest.session_id,
                _model_settings_repr,
            )
            logger.info(
                "[DEEPSEEK-DEBUG] pre-call session=%s no extra_headers configured by bernstein "
                "(no HTTP-Referer/X-Title sent to OpenRouter unless the SDK's own default "
                "HEADERS constant includes them) - client_kwargs base_url=%r",
                manifest.session_id,
                client_kwargs.get("base_url"),
            )
            try:
                _tool_schema_summary = _deepseek_debug_tool_schema_summary(_tool_list_for_log)
                logger.info(
                    "[DEEPSEEK-DEBUG] pre-call session=%s tool_schemas=%s",
                    manifest.session_id,
                    json.dumps(_tool_schema_summary, default=str)[:8000],
                )
                _any_strict_true = any(entry.get("strict_json_schema") is True for entry in _tool_schema_summary)
                if _any_strict_true:
                    logger.warning(
                        "[DEEPSEEK-DEBUG] pre-call session=%s: at least one tool schema has "
                        "strict_json_schema=True - the OpenAI Agents SDK sends this as "
                        "'strict': true in the ChatCompletionToolParam.function block "
                        "(agents/models/chatcmpl_converter.py Converter.tool_to_openai). "
                        "This is the leading hypothesis for deepseek-chat-via-OpenRouter "
                        "empty/malformed tool_calls - deepseek's function calling is not "
                        "confirmed to fully support OpenAI's strict structured-output mode.",
                        manifest.session_id,
                    )
            except Exception as exc:  # diagnostics only - never mask the real failure
                logger.warning(
                    "[DEEPSEEK-DEBUG] pre-call session=%s: tool schema logging itself failed: %s: %s",
                    manifest.session_id,
                    type(exc).__name__,
                    exc,
                )

            # ``Runner.run_sync`` is the SDK's synchronous API - we avoid
            # ``asyncio.run`` here so the runner stays compatible with
            # environments where the event loop is already running
            # (e.g. pytest-asyncio tests that import this module). ``max_turns``
            # is only forwarded when configured (env/tuning) - omitting the
            # kwarg preserves the SDK's own default exactly, as before.
            result = runner_cls.run_sync(agent, manifest.prompt, **run_sync_kwargs)

            # [DEEPSEEK-DEBUG] Unconditional post-call diagnostic: dump as much
            # of the raw SDK response shape as is discoverable so an empty
            # completion (zero usage, empty summary) or a malformed-tool-call
            # response is a direct log read instead of a re-run-and-guess.
            try:
                _raw_responses = getattr(result, "raw_responses", None) or []
                _raw_summaries: list[dict[str, Any]] = []
                for _rr in _raw_responses:
                    _rr_usage = getattr(_rr, "usage", None)
                    _output = getattr(_rr, "output", None)
                    _choices = getattr(_rr, "choices", None)
                    _summary: dict[str, Any] = {
                        "usage": {
                            "input_tokens": getattr(_rr_usage, "input_tokens", None),
                            "output_tokens": getattr(_rr_usage, "output_tokens", None),
                            "raw": str(_rr_usage) if _rr_usage is not None else None,
                        },
                    }
                    if _choices is not None:
                        _choice_summaries = []
                        for _choice in _choices:
                            _message = getattr(_choice, "message", None)
                            _choice_summaries.append(
                                {
                                    "finish_reason": getattr(_choice, "finish_reason", None),
                                    "content": getattr(_message, "content", None) if _message else None,
                                    "refusal": getattr(_message, "refusal", None) if _message else None,
                                    "tool_calls": str(getattr(_message, "tool_calls", None)) if _message else None,
                                },
                            )
                        _summary["choices"] = _choice_summaries
                    if _output is not None:
                        _summary["output"] = str(_output)[:4000]
                    _raw_summaries.append(_summary)
                logger.info(
                    "[DEEPSEEK-DEBUG] post-call session=%s model=%s raw_responses_count=%d "
                    "final_output=%r raw_responses=%s",
                    manifest.session_id,
                    manifest.model,
                    len(_raw_responses),
                    str(getattr(result, "final_output", ""))[:2000],
                    json.dumps(_raw_summaries, default=str)[:8000],
                )
                _result_usage = getattr(result, "usage", None)
                logger.info(
                    "[DEEPSEEK-DEBUG] post-call session=%s result.usage=%r "
                    "(RunResult/RunResultBase does not define 'usage' on installed SDK "
                    "0.17.7 per _extract_usage_tokens's docstring - expect None here; "
                    "the raw_responses per-call usage above is the real signal)",
                    manifest.session_id,
                    _result_usage,
                )
            except Exception as exc:  # diagnostics only - never mask the real failure
                logger.warning(
                    "[DEEPSEEK-DEBUG] post-call session=%s: raw response logging itself "
                    "failed (SDK shape may differ from expected): %s: %s",
                    manifest.session_id,
                    type(exc).__name__,
                    exc,
                )

            # Wave 3 (per-agent instrumentation): log the full conversation
            # (input items + generated items + final output) from this run
            # to conversation.jsonl - see _log_result_conversation_messages.
            # Wave 4: skipped when the per-turn RunHooks path already logged
            # this session's turns in REAL TIME via _log_hook_turn_conversation
            # (see on_llm_end in _build_instrumentation_hooks) - running both
            # would duplicate every message that already has a real-time entry.
            if _hook_llm_calls_logged == 0:
                _log_result_conversation_messages(result)
            else:
                logger.debug(
                    "skipping post-hoc _log_result_conversation_messages for session=%s: "
                    "%d turn(s) already logged in real time via RunHooks",
                    manifest.session_id,
                    _hook_llm_calls_logged,
                )
    except Exception as exc:  # SDK errors are varied - catch broadly
        # D2 MiniMax attempt-3 (2026-07-03): agents that die on an
        # exception - especially ``MaxTurnsExceeded``, which by definition
        # fires AFTER ``max_turns`` real, billable LLM calls - previously
        # emitted zero usage events, so an entire failed run metered
        # ``spent_usd: 0.0, usages: []`` despite real provider spend.
        # SDK exceptions derived from ``AgentsException`` carry the partial
        # run's state on ``exc.run_data`` (``RunErrorDetails``, with the
        # same ``raw_responses`` list a successful ``RunResult`` has), so
        # extract and price whatever usage exists before returning; when
        # the exception carries no run data, ``_emit_session_usage`` still
        # emits the ``usage_missing`` marker so downstream sees "unknown",
        # never a silent zero.
        #
        # [DEEPSEEK-DEBUG] Before this fix, everything downstream of this
        # except block only ever saw ``f"{type(exc).__name__}: {exc}"`` -
        # the full traceback was silently swallowed. Log it unconditionally
        # at ERROR so an exception raised mid-call (e.g. a malformed
        # response the SDK's client-side parsing chokes on) is a direct log
        # read, not a guess from a one-line message.
        logger.exception(
            "[DEEPSEEK-DEBUG] exception in Runner.run_sync session=%s model=%s base_url=%r exc_type=%s",
            manifest.session_id,
            manifest.model,
            manifest.base_url,
            type(exc).__name__,
        )
        _emit_session_usage(
            manifest,
            getattr(exc, "run_data", None),
            source_desc=f"{type(exc).__name__}.run_data",
        )
        if type(exc).__name__ == "MaxTurnsExceeded":
            _log_max_turns_exceeded(exc, max_turns)
        # Logging-gap fix: conversation.jsonl was previously only ever
        # written on the success path (see the call to
        # ``_log_result_conversation_messages`` after ``run_sync`` returns
        # below). Any run that raises - MaxTurnsExceeded, a rate limit, a
        # malformed SDK response - skipped straight to the ``except`` block
        # and never logged a single conversation message, even though
        # ``exc.run_data`` (``RunErrorDetails``) carries the same
        # ``new_items``/``final_output`` shape a successful ``RunResult``
        # does (this is exactly the object ``_emit_session_usage`` above
        # already reads for usage/pricing on the failure path). Mirror that
        # partial state into conversation.jsonl too so a failed run leaves
        # a full transcript of what happened before it died, not silence.
        # Wave 4: skipped when RunHooks already logged this session's
        # completed turns in real time (see the success-path guard above for
        # the identical reasoning) - only the turns that ran before the
        # exception fired were ever hook-logged, but that is exactly the
        # partial transcript this post-hoc call would otherwise re-derive
        # from exc.run_data, so re-running it here would duplicate those
        # entries rather than adding new ones.
        if _hook_llm_calls_logged == 0:
            _log_result_conversation_messages(getattr(exc, "run_data", None))
        else:
            logger.debug(
                "skipping post-hoc _log_result_conversation_messages on exception path "
                "for session=%s: %d turn(s) already logged in real time via RunHooks",
                manifest.session_id,
                _hook_llm_calls_logged,
            )
        if _is_rate_limit(exc):
            emit_event(
                {
                    "type": "error",
                    "kind": "rate_limit",
                    "message": str(exc),
                },
            )
            return EXIT_RATE_LIMIT
        emit_event(
            {
                "type": "error",
                "kind": "runtime",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
        return EXIT_GENERIC

    # Bug 13 follow-up (2026-07-02): a real MiniMax-M2.7-highspeed smoke run
    # proved ``result.usage`` is ``None`` on this provider - installed SDK
    # inspection (openai-agents 0.17.7, ``agents/result.py``) shows
    # ``RunResult``/``RunResultBase`` never define a ``usage`` attribute at
    # all, so the ``raw_responses`` fallback inside ``_extract_usage_tokens``
    # is the path that actually fires on real SDK result objects.
    #
    # Council fix: ``manifest.model`` for a council role names the
    # ``.yaml`` config file, not a real model id, so pricing the whole
    # council under it looked up the yaml path in the pricing table and
    # metered at $0 with a warning. When the council ran, ``result`` is a
    # ``CouncilRunResult`` carrying ``member_usage`` - one entry per live
    # candidate plus the judge, each with its own real model id and its
    # own result object (see
    # ``bernstein.adapters.council_runner.CouncilRunResult``). Emit one
    # usage event per member, priced under that member's real model,
    # instead of one aggregate event priced under the yaml path.
    member_usage = getattr(result, "member_usage", None) if manifest.council is not None else None
    if member_usage:
        for member in member_usage:
            _emit_session_usage(
                manifest,
                member.get("result"),
                source_desc=f"council:{member.get('label')}",
                model=member.get("model"),
            )
    else:
        _emit_session_usage(manifest, result, source_desc="result")

    emit_event(
        {
            "type": "completion",
            "status": "done",
            "summary": str(getattr(result, "final_output", "")),
        },
    )
    return EXIT_OK


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments for the runner."""
    parser = argparse.ArgumentParser(
        prog="bernstein.adapters.openai_agents_runner",
        description="Run an OpenAI Agents SDK session from a manifest file.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the JSON manifest written by the adapter.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m bernstein.adapters.openai_agents_runner``.

    Args:
        argv: Command-line arguments excluding ``argv[0]``.  Defaults to
            ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    manifest_path = Path(args.manifest)
    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError:
        emit_event(
            {
                "type": "error",
                "kind": "manifest_missing",
                "message": f"manifest not found: {manifest_path}",
            },
        )
        return EXIT_MANIFEST_ERROR
    except (json.JSONDecodeError, TypeError) as exc:
        emit_event(
            {
                "type": "error",
                "kind": "manifest_invalid",
                "message": f"manifest parse failed: {exc}",
            },
        )
        return EXIT_MANIFEST_ERROR

    return run(manifest)


if __name__ == "__main__":  # pragma: no cover - executed via ``python -m``
    sys.exit(main())
