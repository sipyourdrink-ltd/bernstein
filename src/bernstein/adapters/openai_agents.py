"""OpenAI Agents SDK v2 adapter.

Wraps the ``openai-agents`` SDK (``Agent`` + ``Runner``) in a CLI-spawnable
subprocess so Bernstein's existing spawner can manage lifecycle, timeouts,
rate-limit detection, and cost tracking the same way it does for every other
CLI coding agent.

The SDK itself ships sandboxed execution, filesystem tools, MCP support, and
pluggable sandbox providers (E2B, Modal, Daytona, Cloudflare, Vercel, Runloop,
Blaxel).  Bernstein treats those primitives as adapter-internal: the runner
script constructs ``Agent(...)``, ``Runner.run(...)``, and a
``SandboxRunConfig`` inside a child process; this module is strictly a spawner.

Optional install
----------------

The ``openai-agents`` package is an optional dependency.  Install it with::

    pip install bernstein[openai]

If the package is missing at spawn time the adapter still loads (so
``bernstein agents`` listing and tests work), but ``spawn()`` will fail with
a clear error pointing at the extra.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
)
from bernstein.core.models import ApiTier, ApiTierInfo, ProviderType, RateLimit

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Credential env vars the SDK may read.  Kept in a tuple so
# ``scoped_credential_keys`` can expose them to the credential-scoping
# policy without importing the adapter module at policy-load time.
_OPENAI_CREDENTIAL_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
)

# Default sandbox provider used by the runner.  ``unix_local`` is the
# SDK's out-of-the-box provider that runs tools as subprocesses constrained
# to the workdir Bernstein already passes in.  More capable providers
# (``e2b``, ``modal``, ``docker``) can be selected per spawn via
# ``mcp_config["sandbox_provider"]`` — see the runner script for the
# full list.
_DEFAULT_SANDBOX_PROVIDER: str = "unix_local"

# Models the runner accepts.  Used for ``supported_models`` reporting and
# to map effort tiers back to the cheapest viable SKU.  Entries must
# also appear in ``bernstein.core.cost.cost.MODEL_COSTS_PER_1M_TOKENS``
# so cost tracking can price tool-call usage.
_SUPPORTED_MODELS: tuple[str, ...] = (
    "gpt-5",
    "gpt-5-mini",
    "o4",
    "o4-mini",
)


class OpenAIAgentsAdapter(PluginAdapter):
    """Spawn and monitor OpenAI Agents SDK v2 sessions.

    The adapter does not call the SDK directly.  Instead it spawns
    :mod:`bernstein.adapters.openai_agents_runner` as a Python subprocess,
    piping a JSON manifest on stdin and reading structured JSON events
    line-by-line from stdout.  This keeps the SDK import out of the
    orchestrator's hot path so users without the optional dependency can
    still import the module for discovery/testing.
    """

    def plugin_info(self) -> AdapterPluginInfo:
        """Return metadata for the ``bernstein agents`` listing."""
        return AdapterPluginInfo(
            name="openai_agents",
            version="0.1.0",
            author="bernstein",
            description="Orchestrate agents built on OpenAI Agents SDK v2",
            homepage="https://openai.github.io/openai-agents-python/",
            min_bernstein_version="1.9.0",
            capabilities=(
                AdapterCapability.STREAMING,
                AdapterCapability.TOOL_USE,
                AdapterCapability.MULTI_MODEL,
                AdapterCapability.RATE_LIMIT_DETECTION,
                AdapterCapability.STRUCTURED_OUTPUT,
            ),
        )

    def supported_models(self) -> list[str]:
        """Return the tuple of OpenAI model IDs the runner accepts."""
        return list(_SUPPORTED_MODELS)

    def health_check(self) -> bool:
        """Return True when the ``openai-agents`` SDK can be imported.

        The adapter module itself must stay importable without the SDK so
        ``bernstein agents`` can list capabilities even when the optional
        extra is not installed.  Health checks answer the stronger
        question of whether :meth:`spawn` would actually succeed.
        """
        try:
            import importlib.util

            return importlib.util.find_spec("agents") is not None
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("openai_agents health_check import probe failed: %s", exc)
            return False

    def scoped_credential_keys(self) -> tuple[str, ...]:
        """Return the env-var keys this adapter is allowed to read.

        Consumed by :mod:`bernstein.core.credential_scoping` to build the
        per-agent policy used by :func:`build_filtered_env`.
        """
        return _OPENAI_CREDENTIAL_KEYS

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_manifest(
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None,
        timeout_seconds: int,
        task_scope: str,
        budget_multiplier: float,
        system_addendum: str,
    ) -> dict[str, Any]:
        """Serialize spawn parameters into the runner's stdin manifest.

        The manifest schema is an adapter-internal contract — any field
        added here must also be consumed by ``openai_agents_runner``.
        ``mcp_config`` is passed through unchanged so MCP servers that
        Bernstein already manages (bernstein bridge, user-configured
        servers) stay under Bernstein's control rather than being
        re-spawned by the OpenAI SDK.

        Args:
            prompt: Task prompt forwarded to ``Runner.run``.
            workdir: Worktree root for sandbox constraint.
            model_config: Model/effort selection.
            session_id: Bernstein session ID for log correlation.
            mcp_config: Optional MCP servers and sandbox provider choice.
            timeout_seconds: Hard timeout forwarded to the runner.
            task_scope: "small" | "medium" | "large".
            budget_multiplier: Retry multiplier applied to the scope budget.
            system_addendum: Orchestration context injected as system prompt.

        Returns:
            Plain dict ready for ``json.dumps``.
        """
        sandbox_provider = _DEFAULT_SANDBOX_PROVIDER
        tools: list[dict[str, Any]] = []
        mcp_servers: dict[str, Any] = {}
        if mcp_config:
            provider = mcp_config.get("sandbox_provider")
            if isinstance(provider, str) and provider:
                sandbox_provider = provider
            raw_tools: object = mcp_config.get("tools")
            if isinstance(raw_tools, list):
                tools = [cast("dict[str, Any]", t) for t in cast("list[Any]", raw_tools) if isinstance(t, dict)]
            raw_servers: object = mcp_config.get("mcpServers")
            if isinstance(raw_servers, dict):
                mcp_servers = cast("dict[str, Any]", raw_servers)

        return {
            "session_id": session_id,
            "prompt": prompt,
            "workdir": str(workdir),
            "model": str(getattr(model_config, "model", "")),
            "effort": str(getattr(model_config, "effort", "high")),
            "max_tokens": int(getattr(model_config, "max_tokens", 200_000)),
            "timeout_seconds": timeout_seconds,
            "task_scope": task_scope,
            "budget_multiplier": budget_multiplier,
            "system_addendum": system_addendum,
            "sandbox_provider": sandbox_provider,
            "tools": tools,
            "mcp_servers": mcp_servers,
        }

    @staticmethod
    def _runner_command() -> list[str]:
        """Return the command that invokes the runner module."""
        return [sys.executable, "-m", "bernstein.adapters.openai_agents_runner"]

    # ------------------------------------------------------------------
    # Public API — CLIAdapter contract
    # ------------------------------------------------------------------

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
    ) -> SpawnResult:
        """Launch the OpenAI Agents runner subprocess.

        Returns a :class:`SpawnResult` pointing at the subprocess PID and
        its log file.  The runner writes one structured JSON event per
        line to stdout; the spawner collects those events via the log
        file and Bernstein's existing log tail/hook machinery.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        manifest_path = workdir / ".sdd" / "runtime" / f"{session_id}.manifest.json"
        manifest = self._build_manifest(
            prompt=prompt,
            workdir=workdir,
            model_config=model_config,
            session_id=session_id,
            mcp_config=mcp_config,
            timeout_seconds=timeout_seconds,
            task_scope=task_scope,
            budget_multiplier=budget_multiplier,
            system_addendum=system_addendum,
        )
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if not os.environ.get("OPENAI_API_KEY"):
            logger.warning(
                "OpenAIAgentsAdapter: OPENAI_API_KEY is not set — spawn will fail",
            )

        cmd = [*self._runner_command(), "--manifest", str(manifest_path)]

        # Wrap with bernstein-worker for process visibility.
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=str(getattr(model_config, "model", "")),
        )

        env = build_filtered_env(list(_OPENAI_CREDENTIAL_KEYS))
        preexec_fn = self._get_preexec_fn()
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "python executable not found for openai-agents runner. "
                    "Reinstall Bernstein or verify sys.executable.",
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(
                    f"Permission denied executing openai-agents runner: {exc}",
                ) from exc

        self._probe_fast_exit(proc, log_path, provider_name="openai_agents")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(
                proc.pid,
                timeout_seconds,
                session_id,
            )
        return result

    def name(self) -> str:
        """Human-readable adapter name."""
        return "OpenAI Agents SDK"

    # ------------------------------------------------------------------
    # Provider tier detection
    # ------------------------------------------------------------------

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect the OpenAI tier from environment configuration.

        Reuses the same heuristics as the :class:`CodexAdapter` because
        both live on the OpenAI platform: the presence of
        ``OPENAI_ORGANIZATION`` implies Enterprise, ``sk-proj...`` implies
        Pro, and any other ``sk-...`` key is treated as Plus.

        Returns:
            :class:`ApiTierInfo` when an API key is present, otherwise
            ``None``.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "")
        org_id = os.environ.get("OPENAI_ORGANIZATION", "") or os.environ.get(
            "OPENAI_ORG_ID",
            "",
        )

        if not api_key:
            return None

        if org_id:
            tier = ApiTier.ENTERPRISE
            rate_limit = RateLimit(requests_per_minute=500, tokens_per_minute=90_000)
        elif api_key.startswith("sk-proj"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(requests_per_minute=100, tokens_per_minute=10_000)
        elif api_key.startswith("sk-"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(requests_per_minute=60, tokens_per_minute=5_000)
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(requests_per_minute=20, tokens_per_minute=2_000)

        return ApiTierInfo(
            provider=ProviderType.CODEX,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
