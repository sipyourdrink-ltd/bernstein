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
    {"type": "usage", "input_tokens": 123, "output_tokens": 456, "tool_calls": 3}
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Exit codes are part of the public contract with the adapter - keep in sync
# with the module docstring above.
EXIT_OK: int = 0
EXIT_GENERIC: int = 1
EXIT_MANIFEST_ERROR: int = 2
EXIT_SDK_MISSING: int = 3
EXIT_RATE_LIMIT: int = 4

# Env var overriding AGENT.max_turns (tuning.agent.max_turns in bernstein.yaml).
# Checked first; an unset/blank/unparseable value falls through to the
# yaml-tunable default, which itself defaults to ``None`` (SDK default of 10
# applies, unchanged from prior behavior).
MAX_TURNS_ENV_VAR: str = "BERNSTEIN_MAX_TURNS"


def _resolve_max_turns() -> int | None:
    """Resolve the effective ``max_turns`` for ``Runner.run_sync``.

    Precedence: ``BERNSTEIN_MAX_TURNS`` env var > ``AGENT.max_turns`` (yaml
    ``tuning.agent.max_turns``) > ``None`` (SDK default applies - unchanged
    behavior for anyone not opting in).
    """
    raw_env = os.environ.get(MAX_TURNS_ENV_VAR)
    if raw_env is not None and raw_env.strip():
        try:
            return int(raw_env)
        except ValueError:
            logger.warning(
                "%s=%r is not a valid int; falling back to tuning.agent.max_turns/SDK default",
                MAX_TURNS_ENV_VAR,
                raw_env,
            )

    from bernstein.core.defaults import AGENT

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


def _build_run_config(manifest: RunnerManifest) -> dict[str, Any]:
    """Build the SDK ``RunConfig`` / ``SandboxRunConfig`` shape.

    Returns a plain dict so the caller can hand the pieces to the SDK
    without this module having to import SDK types.
    """
    return {
        "sandbox_provider": manifest.sandbox_provider,
        "workdir": manifest.workdir,
        "timeout_seconds": manifest.timeout_seconds,
        "mcp_servers": manifest.mcp_servers.copy(),
    }


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

    try:
        agent_kwargs = _build_agent_kwargs(manifest)
        if manifest.tool_source == "builtin":
            # Opt-in workdir-sandboxed builtins for runs with no MCP gateway
            # reachable. Every call is recorded to this same event stream via
            # ``emit_event`` so the run stays auditable and replayable.
            from bernstein.adapters.openai_agents_builtins import (
                build_builtin_tools,
                selected_builtin_names,
            )

            active_names = selected_builtin_names(manifest.sandbox_provider)
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
            )
        settings_kwargs = _build_model_settings_kwargs(manifest, model_settings_cls=sdk.ModelSettings)
        if settings_kwargs:
            agent_kwargs["model_settings"] = sdk.ModelSettings(**settings_kwargs)
        agent: Any = agent_cls(**agent_kwargs)
        run_config = _build_run_config(manifest)
        run_sync_kwargs: dict[str, Any] = {"run_config": run_config}
        max_turns = _resolve_max_turns()
        if max_turns is not None:
            run_sync_kwargs["max_turns"] = max_turns
        # ``Runner.run_sync`` is the SDK's synchronous API - we avoid
        # ``asyncio.run`` here so the runner stays compatible with
        # environments where the event loop is already running
        # (e.g. pytest-asyncio tests that import this module). ``max_turns``
        # is only forwarded when configured (env/tuning) - omitting the
        # kwarg preserves the SDK's own default exactly, as before.
        result: Any = runner_cls.run_sync(agent, manifest.prompt, **run_sync_kwargs)
    except Exception as exc:  # SDK errors are varied - catch broadly
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

    usage: Any = getattr(result, "usage", None)
    if usage is not None:
        emit_event(
            {
                "type": "usage",
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "tool_calls": int(getattr(usage, "tool_calls", 0) or 0),
            },
        )

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
