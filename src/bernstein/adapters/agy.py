"""Antigravity CLI (``agy``) adapter.

``agy`` is the successor CLI for the hosted backend that the legacy
``gemini`` binary served on the free / AI Pro / Ultra path; that backend
was discontinued for non-enterprise users in June 2026. The gemini
adapter remains registered for enterprise and API-key operators, so the
two lanes stay separate:

* ``agy`` -- this adapter. Single closed-source binary, headless
  ``-p``/``--print`` invocation, terminal sandbox pinned, permission
  prompts auto-approved for unattended runs.
* ``gemini`` -- the dual-binary adapter for the legacy ``gemini`` /
  transitional ``antigravity`` binaries (enterprise / API-key lane).

Verified against Antigravity CLI 1.0.0. The upstream surface the adapter
relies on (and that the conformance contract pins):

* ``-p`` / ``--print`` -- run a single prompt non-interactively and
  print the response (the headless harness path).
* ``--sandbox`` -- run with terminal restrictions enabled; pinned on
  every spawn so unattended runs stay inside the sandbox.
* ``--dangerously-skip-permissions`` -- auto-approve tool permission
  requests; required for unattended operation.
* ``--print-timeout`` -- upstream-side wait bound for print mode; the
  adapter maps ``timeout_seconds`` onto it so the CLI's own timeout
  agrees with Bernstein's watchdog.
* ``--conversation <id>`` -- resume a previous conversation by id
  (upstream-minted; native resume is not wired yet, so the strategy
  matrix declares resume as unsupported and the orchestrator falls back
  to fresh sessions with scratchpad reinjection).

The CLI accepts ``--model`` for explicit model selection. Bernstein's
``default`` sentinel omits that flag so zero-config runs retain the CLI's
server-side default.

Binary discovery is a two-step cascade:

1. The operator override ``BERNSTEIN_AGY_BINARY`` wins when set (and
   must resolve on ``PATH``, regardless of ``strict`` mode).
2. Otherwise the ``agy`` binary is used.

``strict=True`` (doctor / ``bernstein adapters check``) raises
:class:`AgyBinaryNotInstalledError` when nothing resolves;
``strict=False`` (spawn path, default) returns the default binary name so
the downstream :func:`subprocess.Popen` surfaces the natural
``FileNotFoundError`` -- the codex / aider posture, which also keeps
tests that mock ``subprocess`` from tripping on eager discovery.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnError,
    SpawnResult,
    build_worker_cmd,
)
from bernstein.adapters.env_isolation import build_filtered_env

logger = logging.getLogger(__name__)

#: Operator override env var for the binary name. Takes precedence over
#: discovery so operators on a non-default install path do not have to
#: rely on ``PATH`` ordering.
BINARY_ENV_VAR: str = "BERNSTEIN_AGY_BINARY"

#: Default binary name for the Antigravity CLI.
AGY_BINARY: str = "agy"


class AgyBinaryNotInstalledError(SpawnError):
    """Raised when the ``agy`` binary cannot be resolved on ``PATH``.

    The message names the expected binary and the override env var so
    the operator-facing error is self-explanatory without consulting
    the docs.
    """


def resolve_agy_binary(
    *,
    which: Any = None,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> str:
    """Resolve the Antigravity CLI binary to invoke.

    Args:
        which: Callable matching :func:`shutil.which`. Tests override
            this to simulate ``PATH`` contents without touching the
            real filesystem.
        env: Environment mapping to consult for
            :data:`BINARY_ENV_VAR`. Defaults to :data:`os.environ`.
        strict: When ``True`` (doctor / ``bernstein adapters check``)
            raise :class:`AgyBinaryNotInstalledError` if the binary
            does not resolve. When ``False`` (default, spawn path)
            return :data:`AGY_BINARY` so the downstream
            :func:`subprocess.Popen` raises the natural
            ``FileNotFoundError`` if the binary is genuinely missing.

    Returns:
        The binary name to invoke. Operator override (when set and
        non-empty) wins; otherwise :data:`AGY_BINARY`.

    Raises:
        AgyBinaryNotInstalledError: When ``strict=True`` and the binary
            does not resolve, OR when the override is set but missing
            on ``PATH`` (regardless of ``strict``, since this is
            operator error).
    """
    source_env = env if env is not None else os.environ
    # Resolve ``shutil.which`` at call time so tests that patch
    # ``bernstein.adapters.agy.shutil.which`` see the swap.
    resolver = which if which is not None else shutil.which
    override = (source_env.get(BINARY_ENV_VAR) or "").strip()
    if override:
        if resolver(override) is None:
            raise AgyBinaryNotInstalledError(
                f"{BINARY_ENV_VAR}={override!r} but {override!r} is not on PATH. "
                f"Unset {BINARY_ENV_VAR} to fall back to discovery, or install the binary."
            )
        return override

    if resolver(AGY_BINARY) is not None:
        return AGY_BINARY

    if strict:
        raise AgyBinaryNotInstalledError(
            "The 'agy' binary was not found on PATH. Install the Antigravity CLI "
            f"(see docs/adapters/agy.md), or set {BINARY_ENV_VAR}=<path-or-name> "
            "to override discovery."
        )
    # Non-strict mode: return the default name so the call site
    # (typically subprocess.Popen) surfaces the missing binary as a
    # natural FileNotFoundError it can already handle.
    return AGY_BINARY


class AgyAdapter(CLIAdapter):
    """Spawn and monitor Antigravity CLI (``agy``) sessions.

    The harness path is the CLI's print mode:
    ``agy -p <prompt> --sandbox --dangerously-skip-permissions
    [--print-timeout <N>s]``. ``-p`` runs a single prompt
    non-interactively and prints the response, ``--sandbox`` pins the
    terminal-restriction sandbox so unattended runs cannot break out of
    the workspace, and ``--dangerously-skip-permissions`` auto-approves
    tool permission prompts so the run never blocks. ``timeout_seconds``
    is mirrored onto ``--print-timeout`` so the upstream wait bound
    agrees with Bernstein's watchdog.
    """

    registry_name = "agy"

    # Provider-string aliases resolved via the registry's provider-alias
    # table. ``agy`` only -- the ``gemini`` / ``google`` aliases stay with
    # the gemini adapter so the enterprise / API-key lane keeps routing
    # to the legacy binary cascade.
    provides = ("agy",)

    # This is the documented sentinel for "the backend picks the model" - the
    # same value the canary matrix pins for this adapter - and exists so the
    # spawner's tier-name
    # guard can resolve a spawnable model config on zero-config runs instead
    # of refusing to spawn (issue #2743).
    default_model = "default"

    external_endpoints = (("generativelanguage.googleapis.com", 443),)
    # The hosted backend returns HTTP 429 with ``RESOURCE_EXHAUSTED``
    # once per-minute quotas trip; same meter label as the gemini lane.
    rate_limit_provider = "google_generative_language"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Launch an Antigravity CLI session in print (headless) mode.

        Args:
            prompt: The prompt supplied via ``-p``; the CLI exits when done.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration. Explicit models are
                passed to the CLI; ``default`` retains server-side selection.
            session_id: Unique session identifier (log naming and worker
                bookkeeping; the CLI mints its own conversation id).
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds; also mirrored
                onto ``--print-timeout`` when positive.
            task_scope: Task scope hint (unused by agy).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).
            multimodal_context: Multimodal attachments; refused until the
                agy attachment surface is conformance-covered.

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``agy`` binary is missing from PATH or
                cannot be executed.
        """
        self.enforce_network_policy()
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        binary = resolve_agy_binary()

        cmd = [
            binary,
            "-p",
            prompt,
            "--sandbox",
            "--dangerously-skip-permissions",
        ]
        if model_config.model != self.default_model:
            cmd.extend(["--model", model_config.model])
        if timeout_seconds > 0:
            # Mirror the watchdog bound onto the CLI's own print-mode
            # timeout (Go duration syntax) so both sides agree.
            cmd.extend(["--print-timeout", f"{timeout_seconds}s"])

        # Wrap with bernstein-worker for process visibility.
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        # Auth is keyring / OAuth by default; API keys are optional
        # overrides shared with the gemini lane.
        env = build_filtered_env(
            [
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_APPLICATION_CREDENTIALS",
            ]
        )

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"{binary} not found in PATH. Install the Antigravity CLI; see docs/adapters/agy.md."
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {binary}: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name=binary)

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Antigravity"


__all__ = [
    "AGY_BINARY",
    "BINARY_ENV_VAR",
    "AgyAdapter",
    "AgyBinaryNotInstalledError",
    "resolve_agy_binary",
]
