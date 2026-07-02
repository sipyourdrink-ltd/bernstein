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
            SDK default.  ``None`` keeps today's single-endpoint behavior.
        api_key_env: Optional NAME of the environment variable holding the
            API key for ``base_url``.  Never a literal key.  When set but
            the variable is missing the runner fails at startup with
            :data:`EXIT_MANIFEST_ERROR`.
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
    mcp_servers: dict[str, Any] = field(default_factory=dict[str, Any])
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    base_url: str | None = None
    api_key_env: str | None = None

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
    if manifest.tools:
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


def _build_model_settings_kwargs(manifest: RunnerManifest) -> dict[str, Any]:
    """Translate optional sampling params into ``ModelSettings`` kwargs.

    Only fields present in the manifest are emitted so an all-``None``
    manifest yields an empty dict and the runner skips ``ModelSettings``
    entirely (exactly today's behavior).  ``top_k`` is not a first-class
    ``ModelSettings`` field in the SDK, so it travels via ``extra_args``
    for OpenAI-compatible endpoints that accept it.

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
    return kwargs


def _resolve_client_kwargs(manifest: RunnerManifest) -> dict[str, Any]:
    """Build ``AsyncOpenAI(...)`` kwargs for the optional endpoint override.

    Returns an empty dict when neither ``base_url`` nor ``api_key_env`` is
    set, in which case the runner leaves the SDK's default client alone.
    The key is resolved from the environment by NAME - the manifest never
    carries a literal secret.

    Raises:
        RuntimeError: ``api_key_env`` is set but the named environment
            variable is missing or empty.
    """
    kwargs: dict[str, Any] = {}
    if manifest.base_url:
        kwargs["base_url"] = manifest.base_url
    if manifest.api_key_env:
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


def _start_heartbeat(
    session_id: str,
    workdir: Path,
    interval_s: float = 15.0,
) -> threading.Event:
    """Write heartbeat files while the runner process is alive.

    Mirrors the payload schema of ``_start_heartbeat_proxy`` in
    :mod:`bernstein.core.agents.spawner_sandbox_session` so the
    orchestrator's ``HeartbeatMonitor`` treats runner sessions exactly
    like sandbox sessions.

    Args:
        session_id: Runner session identifier (heartbeat file basename).
        workdir: Working directory (project root) where
            ``.sdd/runtime/heartbeats/`` lives.
        interval_s: Seconds between heartbeat writes (default 15).

    Returns:
        A :class:`threading.Event` the caller sets to stop the writer.
    """
    stop_event = threading.Event()
    heartbeat_dir = workdir / ".sdd" / "runtime" / "heartbeats"

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

    heartbeat_stop = _start_heartbeat(manifest.session_id, Path(manifest.workdir))
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

            sdk.set_default_openai_client(AsyncOpenAI(**client_kwargs))
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
        settings_kwargs = _build_model_settings_kwargs(manifest)
        if settings_kwargs:
            agent_kwargs["model_settings"] = sdk.ModelSettings(**settings_kwargs)
        agent: Any = agent_cls(**agent_kwargs)
        run_config = _build_run_config(manifest)
        # ``Runner.run_sync`` is the SDK's synchronous API - we avoid
        # ``asyncio.run`` here so the runner stays compatible with
        # environments where the event loop is already running
        # (e.g. pytest-asyncio tests that import this module).
        result: Any = runner_cls.run_sync(agent, manifest.prompt, run_config=run_config)
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
