"""OpenClaw CLI adapter.

Runs one embedded OpenClaw agent turn per task with ``openclaw agent exec``,
the headless entry point that needs no Gateway daemon. ``--cwd`` makes the
task's worktree both the agent workspace and the tool working directory, so
the unit of work is the diff it leaves there. ``--json`` prints one result
envelope (final text, usage, cost, tool summary) to stdout; exit code 0 is
success, 1 a model or result error, 2 OpenClaw's own timeout.

Models: with ``BERNSTEIN_OPENCLAW_OPENAI_BASE_URL`` set, the adapter writes a
per-session config declaring that endpoint as an OpenAI-compatible provider
and runs against exactly that file (``--config``) with ``--auth-env-only``,
so neither the operator's ``~/.openclaw`` config nor stored credentials take
part. The key never touches disk: the config names
``BERNSTEIN_OPENCLAW_OPENAI_API_KEY``, which OpenClaw interpolates from the
environment. Without the endpoint variable, OpenClaw's own configuration
chooses the provider.

Operator settings:

* ``BERNSTEIN_OPENCLAW_OPENAI_BASE_URL`` / ``_OPENAI_API_KEY`` - the gateway
  endpoint and this agent's key (both, or neither);
* ``BERNSTEIN_OPENCLAW_MODEL`` - the gateway model (default: the task's
  model).

See https://docs.openclaw.ai/cli/agent.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

_PREFIX = "BERNSTEIN_OPENCLAW_"
BASE_URL_ENV = _PREFIX + "OPENAI_BASE_URL"
API_KEY_ENV = _PREFIX + "OPENAI_API_KEY"
MODEL_ENV = _PREFIX + "MODEL"
#: Provider id under which the gateway is declared in the generated config.
PROVIDER_ID = "bernstein-gateway"


def gateway_config(base_url: str, model: str) -> dict[str, Any]:
    """OpenClaw config routing ``model`` to ``base_url``; the key stays in the env."""
    return {
        "agents": {"defaults": {"model": {"primary": f"{PROVIDER_ID}/{model}"}}},
        "models": {
            "providers": {
                PROVIDER_ID: {
                    "baseUrl": base_url,
                    "apiKey": "${" + API_KEY_ENV + "}",
                    "api": "openai-completions",
                    "models": [{"id": model, "name": model}],
                }
            }
        },
    }


class OpenClawAdapter(CLIAdapter):
    """Spawn one headless OpenClaw turn (``openclaw agent exec``) per task."""

    def build_command(
        self,
        *,
        prompt_file: Path,
        workdir: Path,
        runtime_dir: Path,
        session_id: str,
        model: str,
        timeout_seconds: int,
        environ: dict[str, str] | os._Environ[str],
    ) -> list[str]:
        """The ``openclaw agent exec`` argv; writes the gateway config when one is set."""
        cmd = ["openclaw", "agent", "exec", "--message-file", str(prompt_file), "--cwd", str(workdir), "--json"]
        base_url = (environ.get(BASE_URL_ENV) or "").strip()
        has_key = bool((environ.get(API_KEY_ENV) or "").strip())
        chosen = (environ.get(MODEL_ENV) or "").strip() or (model if model.lower() != "auto" else "")
        if base_url or has_key:
            missing = [n for n, ok in ((BASE_URL_ENV, base_url), (API_KEY_ENV, has_key), (MODEL_ENV, chosen)) if not ok]
            if missing:
                raise RuntimeError(f"set {', '.join(missing)} to run OpenClaw through the gateway")
            config_path = runtime_dir / f"openclaw-{session_id}.json"
            config_path.write_text(json.dumps(gateway_config(base_url, chosen), indent=2) + "\n", encoding="utf-8")
            cmd += ["--config", str(config_path), "--auth-env-only", "--model", f"{PROVIDER_ID}/{chosen}"]
        elif chosen:
            cmd += ["--model", chosen]
        # Bernstein's watchdog owns the deadline; OpenClaw's own default is 600 s.
        cmd += ["--timeout", str(max(timeout_seconds, 0))]
        return cmd

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
        """Launch one OpenClaw turn on ``prompt`` in ``workdir``.

        Args:
            prompt: Task prompt, written to a file and passed with ``--message-file``.
            workdir: The task's worktree; OpenClaw's workspace and tool cwd.
            model_config: The task's model, used when ``BERNSTEIN_OPENCLAW_MODEL`` is unset.
            session_id: Unique session identifier for the log, prompt and config files.
            mcp_config: Unused. OpenClaw manages its own tools.
            timeout_seconds: Watchdog timeout in seconds, also passed to OpenClaw.
            task_scope: Unused scope hint.
            budget_multiplier: Unused budget multiplier.
            system_addendum: Appended to the prompt file after the task text.

        Returns:
            :class:`SpawnResult` describing the launched process.

        Raises:
            RuntimeError: If ``openclaw`` is missing or not executable, or the
                gateway settings are incomplete.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        runtime_dir = workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_path = runtime_dir / f"{session_id}.log"
        prompt_file = runtime_dir / f"{session_id}.prompt.md"
        prompt_file.write_text(prompt + (f"\n\n{system_addendum}" if system_addendum else ""), encoding="utf-8")

        cmd = self.build_command(
            prompt_file=prompt_file,
            workdir=workdir,
            runtime_dir=runtime_dir,
            session_id=session_id,
            model=model_config.model or "",
            timeout_seconds=timeout_seconds,
            environ=os.environ,
        )
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=runtime_dir / "pids",
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )
        env = build_filtered_env([API_KEY_ENV, "OPENCLAW_STATE_DIR"])
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
                msg = (
                    "openclaw not found in PATH. Install: npm install -g openclaw "
                    "(Node 24.16+) or see https://docs.openclaw.ai/install"
                )
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing openclaw: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "OpenClaw"
