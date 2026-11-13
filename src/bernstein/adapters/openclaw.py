"""Run one embedded OpenClaw CLI turn per Bernstein task."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.persistence.atomic_write import write_atomic_json, write_atomic_text
from bernstein.core.security.network_policy import NetworkPolicyDenied, policy_from_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

BASE_URL_ENV = "BERNSTEIN_OPENCLAW_OPENAI_BASE_URL"
API_KEY_ENV = "BERNSTEIN_OPENCLAW_OPENAI_API_KEY"
MODEL_ENV = "BERNSTEIN_OPENCLAW_MODEL"
PROVIDER_ID = "bernstein-gateway"
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max", "ultra"})
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _gateway_config(base_url: str, model: str) -> dict[str, Any]:
    return {
        "agents": {"defaults": {"model": {"primary": f"{PROVIDER_ID}/{model}"}}},
        "models": {
            "providers": {
                PROVIDER_ID: {
                    "baseUrl": base_url,
                    "apiKey": "${OPENAI_API_KEY}",
                    "api": "openai-completions",
                    "models": [{"id": model, "name": model}],
                }
            }
        },
    }


def resolve_openclaw_route(model_config: ModelConfig) -> tuple[str, str]:
    """Validate the selected model and endpoint before dispatch; return endpoint and key."""
    model = model_config.model.strip()
    if not model or model.lower() == "auto" or model != model_config.model:
        raise ValueError("OpenClaw requires an explicit task model")
    override = os.environ.get(MODEL_ENV, "").strip()
    if override and override != model:
        raise ValueError(f"{MODEL_ENV} must match the task model")
    base_url = os.environ.get(BASE_URL_ENV, "").strip()
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    gateway = bool(base_url or api_key)
    policy = policy_from_env()
    if gateway:
        missing = [name for name, value in ((BASE_URL_ENV, base_url), (API_KEY_ENV, api_key)) if not value]
        if missing:
            raise ValueError(f"set {', '.join(missing)} for the OpenClaw gateway")
        try:
            url = urlsplit(base_url)
            port = url.port
        except ValueError:
            raise ValueError(f"{BASE_URL_ENV} must be an absolute HTTP(S) URL") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(f"{BASE_URL_ENV} must be an absolute HTTP(S) URL without credentials, query, or fragment")
        policy.check(
            url.hostname,
            port if port is not None else (443 if url.scheme == "https" else 80),
            source="adapter:openclaw",
        )
    elif "/" not in model:
        raise ValueError("OpenClaw built-in providers require a provider-qualified task model")
    elif not policy.allow_any:
        raise NetworkPolicyDenied("OpenClaw default provider", source="adapter:openclaw")

    return base_url, api_key


class OpenClawAdapter(CLIAdapter):
    """Launch a single OpenClaw turn with a private prompt and per-run state."""

    registry_name = "openclaw"
    provides = ("openclaw",)

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
        """Start the worker and return its handle.

        The worker reports a missing OpenClaw executable through its log and
        exit status. Invalid gateway settings are rejected before writing files.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", session_id):
            raise ValueError("Invalid OpenClaw session ID")
        workdir = workdir.resolve()
        thinking = model_config.effort.strip().lower()
        if thinking == "normal":
            thinking = "medium"
        if thinking and thinking not in _THINKING_LEVELS:
            raise ValueError("Unsupported OpenClaw thinking level")
        base_url, api_key = resolve_openclaw_route(model_config)
        if mcp_config is not None and "base_url" in mcp_config and mcp_config["base_url"] != base_url:
            raise ValueError("OpenClaw gateway endpoint changed after admission")
        model = model_config.model
        gateway = bool(base_url)

        runtime_dir = workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        # The pinned config resolves auth profiles and plugins before OpenClaw
        # switches to its own temporary state. Keep those lookups private too.
        private_home = tempfile.mkdtemp(prefix="openclaw-", dir=runtime_dir)
        prompt_file = runtime_dir / f"{session_id}.prompt.md"
        write_atomic_text(prompt_file, prompt + (f"\n\n{system_addendum}" if system_addendum else ""))
        cmd = ["openclaw", "agent", "exec", "--message-file", str(prompt_file), "--cwd", str(workdir), "--json"]
        env = build_filtered_env(() if gateway else _PROVIDER_KEYS)
        for name in (
            "HOME",
            "USERPROFILE",
            "OPENCLAW_HOME",
            "OPENCLAW_STATE_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "APPDATA",
            "LOCALAPPDATA",
        ):
            env[name] = private_home
        if gateway:
            config_path = runtime_dir / f"openclaw-{session_id}.json"
            write_atomic_json(config_path, _gateway_config(base_url, model))
            env["OPENAI_API_KEY"] = api_key
            cmd += ["--config", str(config_path)]
            model = f"{PROVIDER_ID}/{model}"
        else:
            cmd += ["--auth-env-only"]
        cmd += ["--model", model]
        if thinking:
            cmd += ["--thinking", thinking]
        # OpenClaw 2026.11.3 and the Bernstein watchdog both use zero
        # to disable their deadlines.
        cmd += ["--timeout", str(max(timeout_seconds, 0))]

        log_path = runtime_dir / f"{session_id}.log"
        wrapped = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=runtime_dir / "pids",
            workdir=workdir,
            log_path=log_path,
            model=model,
        )
        write_atomic_text(log_path, "")
        with log_path.open("a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                wrapped,
                cwd=private_home,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the display name."""
        return "OpenClaw"
