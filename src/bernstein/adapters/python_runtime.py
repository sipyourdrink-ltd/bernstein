"""Generic Python-invoked agent-runtime adapter (#2959).

Drives a Python-invoked agent runtime in a subprocess-isolated worker running
:mod:`bernstein.adapters.python_runtime_runner`. The runtime is named by the
caller (``runtime_module`` + ``runtime_entrypoint``); the runner imports it,
calls the entrypoint inside the task workdir, and writes one JSON object per
line describing the run's boundary (start, result, error) to stdout, which the
worker captures into the session log.

Configuration arrives through the per-spawn ``mcp_config`` mapping, mirroring
:mod:`bernstein.adapters.openai_agents`:

``runtime_module``
    Required. Import path of the module exposing the runtime entrypoint.
``runtime_entrypoint``
    Optional. Attribute name of the callable to invoke; defaults to ``chat``.

There is no permission surface between Bernstein and the runtime - the runner
imports and calls it directly - which is what the declared
``DangerousModeStrategy.ALWAYS_ON`` means for this adapter: selecting it
authorises unattended execution of the configured module. Nothing about the
declaration makes the runtime safe; it records that no approval prompt exists
to skip.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
)

if TYPE_CHECKING:
    from bernstein.core.models import ModelConfig

#: Default entrypoint attribute looked up on the configured runtime module.
DEFAULT_RUNTIME_ENTRYPOINT = "chat"


class RuntimeConfigError(ValueError):
    """Raised when the per-spawn runtime configuration is unusable.

    Raised *before* any process starts so a malformed ``runtime_module`` or
    ``runtime_entrypoint`` surfaces as a configuration failure instead of a
    worker that imports a module literally named ``"None"`` and then reports a
    started session.
    """


def _require_config_str(value: object, key: str) -> str:
    """Return ``value`` as a non-empty string or raise :class:`RuntimeConfigError`.

    Args:
        value: The raw value taken from ``mcp_config``.
        key: The config key the value came from, used in the error message.

    Returns:
        The validated string.

    Raises:
        RuntimeConfigError: When ``value`` is not a string, or is blank.
    """
    if not isinstance(value, str):
        msg = f"python_runtime: {key!r} must be a string, got {type(value).__name__}"
        raise RuntimeConfigError(msg)
    if not value.strip():
        msg = f"python_runtime: {key!r} must not be empty"
        raise RuntimeConfigError(msg)
    return value


class PythonRuntimeAdapter(PluginAdapter):
    """Generic adapter for Python-invoked agent runtimes (#2959).

    Subclasses :class:`PluginAdapter` and spawns a subprocess worker running
    :mod:`bernstein.adapters.python_runtime_runner` in isolation.
    """

    #: Pins the registry key so :meth:`CLIAdapter.strategy` resolves the
    #: ``python_runtime`` row of ``STRATEGY_MATRIX`` rather than falling back
    #: to ``DEFAULT_ADAPTER_STRATEGY``: :meth:`name` lowers to
    #: ``"pythonruntime"``, which is not a matrix key.
    registry_name = "python_runtime"

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name="python_runtime",
            version="1.0.0",
            author="Bernstein",
            description="Generic Python-invoked agent-runtime adapter",
            capabilities=(
                AdapterCapability.TOOL_USE,
                AdapterCapability.MULTI_MODEL,
                AdapterCapability.STREAMING,
            ),
        )

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
        """Launch the configured Python runtime in a worker subprocess.

        Args:
            prompt: The task prompt handed to the runtime entrypoint.
            workdir: Task worktree; the runner's cwd and the ``workdir``
                keyword passed to the entrypoint.
            model_config: Model selection forwarded to the entrypoint.
            session_id: Session identifier used for the log and PID files.
            mcp_config: Per-spawn config carrying ``runtime_module`` and the
                optional ``runtime_entrypoint``.
            timeout_seconds: Watchdog timeout; no watchdog when not positive.
            task_scope: Task scope label (unused).
            budget_multiplier: Budget multiplier (unused).
            system_addendum: Protocol-critical instructions. A Python runtime
                exposes no guaranteed system channel, so the addendum is
                appended to the prompt - the contract fallback - rather than
                dropped.
            multimodal_context: Attachments; refused, this adapter declares no
                multimodal capability.

        Returns:
            A :class:`SpawnResult` describing the spawned worker.

        Raises:
            RuntimeConfigError: If ``runtime_module`` is absent or either
                runtime key is not a non-empty string.
            RuntimeError: If the Python executable cannot be launched.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()

        config = mcp_config or {}
        if "runtime_module" not in config:
            msg = (
                "python_runtime: no 'runtime_module' configured. This adapter drives a "
                "caller-supplied Python runtime and has nothing to run without one; "
                "pass mcp_config={'runtime_module': '<import.path>'}."
            )
            raise RuntimeConfigError(msg)
        runtime_module = _require_config_str(config["runtime_module"], "runtime_module")
        runtime_entrypoint = (
            _require_config_str(config["runtime_entrypoint"], "runtime_entrypoint")
            if "runtime_entrypoint" in config
            else DEFAULT_RUNTIME_ENTRYPOINT
        )

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        runner_script = Path(__file__).parent / "python_runtime_runner.py"

        effective_prompt = f"{prompt}\n\n{system_addendum}" if system_addendum else prompt

        cmd = [
            sys.executable,
            str(runner_script),
            "--prompt",
            effective_prompt,
            "--model",
            model_config.model,
            "--workdir",
            str(workdir),
            "--runtime-module",
            runtime_module,
            "--runtime-entrypoint",
            runtime_entrypoint,
        ]

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

        env = build_filtered_env(["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"])

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
                raise RuntimeError("python executable not found") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing python_runtime: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "PythonRuntime"
