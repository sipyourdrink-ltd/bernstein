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

logger = logging.getLogger(__name__)

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
    # Control knobs resolved by the SPAWN side. They must travel in the
    # manifest because the spawner hands the runner a filtered environment
    # (env_isolation) that strips BERNSTEIN_* control vars - parent-env
    # values never reach this subprocess on their own. ``None`` means the
    # manifest did not carry the field (e.g. a hand-written manifest for a
    # direct invocation) and the runner's own env/defaults apply.
    allow_run_command: bool | None = None
    max_turns: int | None = None

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
        "model": manifest.model,
    }
    if instructions:
        kwargs["instructions"] = instructions
    # Builtin tools are constructed later in ``_run_session`` (they need the
    # SDK's ``function_tool`` and the event sink), so when the manifest opts
    # into them the gateway descriptors are intentionally not attached here.
    if manifest.tool_source != "builtin" and manifest.tools:
        kwargs["tools"] = manifest.tools.copy()
    return kwargs


def _build_model_settings_kwargs(
    manifest: RunnerManifest,
    *,
    model_settings_cls: type[Any] | None = None,
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
    # completion by POSTing the task /complete endpoint via the run_command
    # builtin tool, so a "/complete" string inside any tool-call item of the
    # partial run means the work was ALREADY done when the cap fired.
    work_completed = "unknown"
    try:
        new_items = getattr(run_data, "new_items", None) or []
        for item in new_items:
            raw_item = getattr(item, "raw_item", item)
            arguments = getattr(raw_item, "arguments", None)
            if isinstance(arguments, str) and "/complete" in arguments:
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


def _emit_session_usage(manifest: RunnerManifest, usage_source: Any, *, source_desc: str) -> None:
    """Extract, price, emit, and sidecar the session's token usage.

    Single choke point for usage accounting, callable from BOTH the success
    path (``usage_source`` = the SDK ``RunResult``) and the exception path
    (``usage_source`` = the exception's ``run_data`` payload, or ``None``).

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
    """
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
            manifest.model,
            source_desc,
        )
        emit_event(
            {
                "type": "usage",
                "input_tokens": 0,
                "output_tokens": 0,
                "tool_calls": tool_calls,
                "model": manifest.model,
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

    price_result = price_model_usage(manifest.model, input_tokens, output_tokens)
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
        manifest.model,
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
            "model": manifest.model,
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
    if client_kwargs:
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
        if explicit_model_client is not None:
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

        settings_kwargs = _build_model_settings_kwargs(manifest, model_settings_cls=sdk.ModelSettings)
        if settings_kwargs:
            agent_kwargs["model_settings"] = sdk.ModelSettings(**settings_kwargs)
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
            "[DEEPSEEK-DEBUG] pre-call session=%s model=%r base_url=%r api_key_env=%r "
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
        result: Any = runner_cls.run_sync(agent, manifest.prompt, **run_sync_kwargs)

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
