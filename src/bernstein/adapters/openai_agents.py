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
from bernstein.adapters.openai_agents_runner import validate_api_key_env_name
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
# ``mcp_config["sandbox_provider"]`` - see the runner script for the
# full list.
_DEFAULT_SANDBOX_PROVIDER: str = "unix_local"

# Operator lever for the runner's tool source.  When set to ``"builtin"``
# every openai_agents spawn uses the runner's workdir-sandboxed builtin
# tools (read_file/write_file/list_dir/run_command) even when the
# per-spawn ``mcp_config`` carries no ``tool_source`` key.  An explicit
# ``mcp_config["tool_source"]`` value always wins over the env var.
# Without this lever nothing in core/ ever populates ``tools`` or sets
# ``tool_source``, so spawns arrive with ZERO tools: the model answers
# the prompt with prose, the SDK sees a final output, and the runner
# exits 0 in seconds having done no work.
TOOL_SOURCE_ENV_VAR: str = "BERNSTEIN_OPENAI_AGENTS_TOOL_SOURCE"

# Kept in sync with ``openai_agents_builtins._ALLOW_RUN_COMMAND_ENV``. The
# spawn side must resolve this itself because ``build_filtered_env`` strips
# BERNSTEIN_* control vars from the runner subprocess environment - a
# parent-env opt-in that is not folded into the manifest simply vanishes.
_ALLOW_RUN_COMMAND_ENV_VAR: str = "BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND"

# Models the runner accepts.  Used for ``supported_models`` reporting and
# to map effort tiers back to the cheapest viable SKU.  Entries must
# also appear in ``bernstein.core.cost.cost.MODEL_COSTS_PER_1M_TOKENS``
# so cost tracking can price tool-call usage.
_SUPPORTED_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.5-mini",
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

    # Provider-string aliases this adapter resolves from in
    # ``_infer_adapter_name_for_provider``. Must resolve ahead of the bare
    # "openai" alias on CodexAdapter, otherwise openai_agents spawns are
    # misrouted to the codex adapter (see 042bcbd0). The registry's
    # exact-match lookup makes ordering irrelevant going forward, but the
    # alias set itself is unchanged from the old substring branch.
    provides = ("openai_agents", "openai-agents")

    # The SDK forwards 429s from api.openai.com with the standard
    # ``rate_limit_exceeded`` / ``insufficient_quota`` error codes.
    rate_limit_provider = "openai"

    # The runner writes its own heartbeat files, but it runs inside a
    # per-session worktree and cannot derive the orchestrator project
    # root on its own.  This flag tells the spawner to inject a
    # ``heartbeat_dir`` key (the orchestrator-root heartbeat directory
    # the HeartbeatMonitor polls) into the per-spawn ``mcp_config``.
    consumes_heartbeat_dir = True

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
                AdapterCapability.SUPPORTS_SAMPLING_PARAMS,
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
        except Exception as exc:  # pragma: no cover - defensive
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

        The manifest schema is an adapter-internal contract - any field
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
            mcp_config: Optional MCP servers, sandbox provider choice,
                sampling/endpoint overrides (``temperature``, ``top_p``,
                ``top_k``, ``base_url``, ``api_key_env``), the tool source
                selector (``tool_source``), the spawner-injected
                ``heartbeat_dir``, and the optional task-level ``council``
                block forwarded from ``role_model_policy.<role>.council``.
            timeout_seconds: Hard timeout forwarded to the runner.
            task_scope: "small" | "medium" | "large".
            budget_multiplier: Retry multiplier applied to the scope budget.
            system_addendum: Orchestration context injected as system prompt.

        Returns:
            Plain dict ready for ``json.dumps``.
        """
        sandbox_provider = _DEFAULT_SANDBOX_PROVIDER
        tool_source = "gateway"
        tools: list[dict[str, Any]] = []
        mcp_servers: dict[str, Any] = {}
        overrides: dict[str, Any] = {}
        if mcp_config:
            provider = mcp_config.get("sandbox_provider")
            if isinstance(provider, str) and provider:
                sandbox_provider = provider
            # ``tool_source: "builtin"`` opts into the runner's
            # workdir-sandboxed builtins.  Any other explicit value keeps
            # the default MCP-gateway path, so the manifest stays
            # byte-identical for runs that do not request builtins.
            raw_tool_source = mcp_config.get("tool_source")
            if raw_tool_source == "builtin":
                tool_source = "builtin"
        # Operator env-var lever: applies only when mcp_config carries no
        # explicit ``tool_source`` (an explicit value - builtin or not -
        # always wins over the environment).
        _env_tool_source_value = os.environ.get(TOOL_SOURCE_ENV_VAR)
        _mcp_config_has_tool_source_key = bool(mcp_config) and "tool_source" in mcp_config
        _env_lever_applies = not _mcp_config_has_tool_source_key and _env_tool_source_value == "builtin"
        if _env_lever_applies:
            tool_source = "builtin"
        logger.info(
            "_build_manifest tool_source decision session=%s: mcp_config_tool_source=%r, "
            "env %s=%r, env_lever_applied=%s -> resolved tool_source=%r",
            session_id,
            (mcp_config or {}).get("tool_source"),
            TOOL_SOURCE_ENV_VAR,
            _env_tool_source_value,
            _env_lever_applies,
            tool_source,
        )
        if mcp_config:
            raw_tools: object = mcp_config.get("tools")
            if isinstance(raw_tools, list):
                tools = [cast("dict[str, Any]", t) for t in cast("list[Any]", raw_tools) if isinstance(t, dict)]
            raw_servers: object = mcp_config.get("mcpServers")
            if isinstance(raw_servers, dict):
                mcp_servers = cast("dict[str, Any]", raw_servers)
            # Optional sampling/endpoint overrides.  Absent keys are not
            # serialized so the runner's defaults (None) apply and the
            # manifest stays byte-identical to pre-override builds.
            for float_key in ("temperature", "top_p"):
                float_value = mcp_config.get(float_key)
                if isinstance(float_value, (int, float)) and not isinstance(float_value, bool):
                    overrides[float_key] = float(float_value)
            top_k = mcp_config.get("top_k")
            if isinstance(top_k, int) and not isinstance(top_k, bool):
                overrides["top_k"] = top_k
            max_tokens = mcp_config.get("max_tokens")
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
                overrides["max_tokens"] = max_tokens
            for str_key in ("base_url", "api_key_env"):
                str_value = mcp_config.get(str_key)
                if isinstance(str_value, str) and str_value:
                    overrides[str_key] = str_value
            # Heartbeat delivery target injected by the spawner: the
            # orchestrator-root directory the HeartbeatMonitor polls.
            # ``workdir`` is a per-session worktree under default
            # isolation, so the runner cannot derive this path itself.
            heartbeat_dir = mcp_config.get("heartbeat_dir")
            if isinstance(heartbeat_dir, str) and heartbeat_dir:
                overrides["heartbeat_dir"] = heartbeat_dir
            # Wave 3 (per-agent instrumentation): task id injected by
            # spawner_core so the runner's RunInstrumenter can write under
            # .sdd/runs/<run_id>/tasks/<task_id>/agents/<session_id>/.
            # Absent on hand-written manifests (e.g. direct-invocation
            # tests) - the runner falls back to "unknown" in that case.
            task_id = mcp_config.get("task_id")
            if isinstance(task_id, str) and task_id:
                overrides["task_id"] = task_id
            # Bug fix (instrumentation audit, bug 3 - "4 of 9 implement
            # tasks have zero instrumentation"): spawner_core batches
            # multiple tasks onto a single agent process for role-batched
            # spawns, but only ever injected ``task_id`` (tasks[0].id) here
            # - every other task in the batch got zero instrumentation
            # coverage since the runner only knew about one task_id. When
            # present, ``task_ids`` carries the full batch so the runner can
            # fan instrumentation out to every task involved (see
            # RunnerManifest.task_ids / RunInstrumenter.extra_dirs).
            task_ids = mcp_config.get("task_ids")
            if isinstance(task_ids, list) and task_ids:
                cleaned_task_ids = [t for t in task_ids if isinstance(t, str) and t]
                if cleaned_task_ids:
                    overrides["task_ids"] = cleaned_task_ids
            # Wave 3 (per-agent instrumentation): orchestrator-root
            # directory injected by spawner_core (mirrors heartbeat_dir
            # above). ``workdir`` is a per-session worktree under default
            # isolation and gets deleted on cleanup/merge - instrumentation
            # JSONL must be anchored to the project root, not the worktree,
            # or the files land somewhere nobody looks and are then
            # deleted with the worktree. Absent on hand-written manifests
            # (e.g. direct-invocation tests) - the runner falls back to
            # ``workdir`` in that case.
            instrumentation_root = mcp_config.get("instrumentation_root")
            if isinstance(instrumentation_root, str) and instrumentation_root:
                overrides["instrumentation_root"] = instrumentation_root
            # Task-level council override injected by spawner_core from an
            # inline ``role_model_policy.<role>.council`` block (already
            # parsed/validated by the seed parser). Forwarded verbatim so
            # ``RunnerManifest.council`` is populated exactly the way the
            # ``model: councils/<name>.yaml`` file convention populates it
            # via ``_load_council_config`` - both paths drive the same
            # ``manifest.council`` branch in the runner.
            council = mcp_config.get("council")
            if isinstance(council, dict) and council:
                overrides["council"] = council

        # ``max_tokens`` from ``mcp_config`` (mode-profile override) wins; the
        # model_config value is only the fallback when the override is absent.
        # It is applied here on the RIGHT of the merge so an ``overrides``
        # value survives, matching the other sampling overrides above.
        overrides.setdefault("max_tokens", int(getattr(model_config, "max_tokens", 200_000)))

        # Control knobs the RUNNER cannot see on its own: the spawner hands
        # the subprocess a filtered environment (env_isolation strips
        # BERNSTEIN_* control vars) and the runner never loads the operator
        # yaml, so these must be resolved HERE - where the parent env and
        # tuning defaults are both visible - and travel in the manifest.
        raw_allow = (mcp_config or {}).get("allow_run_command")
        if isinstance(raw_allow, bool):
            allow_run_command = raw_allow
        else:
            if raw_allow is not None:
                logger.warning(
                    "mcp_config allow_run_command=%r must be a bool; falling back to env %s",
                    raw_allow,
                    _ALLOW_RUN_COMMAND_ENV_VAR,
                )
            allow_run_command = os.environ.get(_ALLOW_RUN_COMMAND_ENV_VAR) == "1"
        raw_max_turns = (mcp_config or {}).get("max_turns")
        if isinstance(raw_max_turns, int) and not isinstance(raw_max_turns, bool) and raw_max_turns > 0:
            max_turns: int | None = raw_max_turns
        else:
            if raw_max_turns is not None:
                logger.warning(
                    "mcp_config max_turns=%r must be a positive int; falling back to "
                    "env/tuning.agent.max_turns/SDK default",
                    raw_max_turns,
                )
            # Spawn-side resolution of env > tuning.agent.max_turns > None,
            # reusing the runner's own resolver (in this process the env is
            # the parent env and defaults are the yaml-loaded tuning).
            from bernstein.adapters.openai_agents_runner import _resolve_max_turns

            max_turns = _resolve_max_turns()

        _tool_names = [str(t.get("name", "<unnamed>")) if isinstance(t, dict) else "<non-dict-tool>" for t in tools]
        logger.info(
            "_build_manifest tool assembly session=%s: resolved tool_source=%r, gateway tool_names=%r "
            "(count=%d), mcp_servers=%r, allow_run_command=%s",
            session_id,
            tool_source,
            _tool_names,
            len(_tool_names),
            list(mcp_servers.keys()),
            allow_run_command,
        )
        # ``tool_source == "builtin"`` legitimately reports zero tools here -
        # the builtin tool objects (read_file/write_file/list_dir, plus
        # run_command when allow_run_command) are constructed later in the
        # runner's ``_run_session``, not put into this manifest's ``tools``
        # list (see openai_agents_runner.py's comment on the builtin path).
        # A "gateway" spawn with zero tools and no mcp servers, however, has
        # NO way to get any tool at all - the agent can only emit text. This
        # is exactly the D2 tools-zero failure mode (silent zero-tools spawn,
        # see work/bernstein/proofs/d2/tools-zero-diagnosis.md).
        if tool_source != "builtin" and not tools and not mcp_servers:
            logger.warning(
                "_build_manifest session=%s: agent will have zero tools - it can only emit text. "
                "reason=gateway tool_source selected but no gateway tools/mcpServers configured "
                "(mcp_config tools=%r, mcpServers=%r; env %s=%r). Set mcp_config['tool_source']='builtin' "
                "or export %s=builtin to grant workdir-sandboxed builtin tools instead.",
                session_id,
                (mcp_config or {}).get("tools"),
                (mcp_config or {}).get("mcpServers"),
                TOOL_SOURCE_ENV_VAR,
                _env_tool_source_value,
                TOOL_SOURCE_ENV_VAR,
            )

        return overrides | {
            "allow_run_command": allow_run_command,
            "max_turns": max_turns,
            "session_id": session_id,
            "prompt": prompt,
            "workdir": str(workdir),
            "model": str(getattr(model_config, "model", "")),
            "effort": str(getattr(model_config, "effort", "high")),
            "timeout_seconds": timeout_seconds,
            "task_scope": task_scope,
            "budget_multiplier": budget_multiplier,
            "system_addendum": system_addendum,
            "sandbox_provider": sandbox_provider,
            "tools": tools,
            "tool_source": tool_source,
            "mcp_servers": mcp_servers,
        }

    @staticmethod
    def _runner_command() -> list[str]:
        """Return the command that invokes the runner module."""
        return [sys.executable, "-m", "bernstein.adapters.openai_agents_runner"]

    # ------------------------------------------------------------------
    # Public API - CLIAdapter contract
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
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Launch the OpenAI Agents runner subprocess.

        Returns a :class:`SpawnResult` pointing at the subprocess PID and
        its log file.  The runner writes one structured JSON event per
        line to stdout; the spawner collects those events via the log
        file and Bernstein's existing log tail/hook machinery.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
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
        # A spawn whose manifest carries tool_source="gateway" with zero
        # tools gives the model NO function tools at all - it can only
        # answer with prose and exit "successfully" having done nothing.
        # Log the effective selection so that failure mode is visible in
        # the orchestrator log instead of only in the (worktree-local,
        # cleanup-prone) runner log.
        logger.info(
            "openai_agents spawn %s: tool_source=%s, tools=%d, model=%s, "
            "allow_run_command=%s, max_turns=%s (env %s=%r, mcp_config tool_source=%r)",
            session_id,
            manifest["tool_source"],
            len(manifest["tools"]),
            manifest["model"],
            manifest["allow_run_command"],
            manifest["max_turns"],
            TOOL_SOURCE_ENV_VAR,
            os.environ.get(TOOL_SOURCE_ENV_VAR),
            (mcp_config or {}).get("tool_source"),
        )

        # ``api_key_env`` overrides which env var holds the key; only its
        # NAME is ever recorded, never the value.  The name is validated
        # BEFORE it widens the filtered environment below so a repo-carried
        # config cannot forward arbitrary host variables to the runner.
        api_key_env_override = manifest.get("api_key_env")
        if api_key_env_override is not None:
            validate_api_key_env_name(str(api_key_env_override))
        # ``key_env_name`` holds only the NAME of the environment variable that
        # carries the credential (e.g. "OPENAI_API_KEY"), never the secret
        # value itself.  It is safe to log.
        key_env_name = str(api_key_env_override or "OPENAI_API_KEY")
        if not os.environ.get(key_env_name):
            logger.warning(
                "OpenAIAgentsAdapter: %s is not set - spawn will fail",
                key_env_name,
            )

        # One-line spawn-manifest summary: model/base_url/api_key_env NAME
        # (never the secret value)/max_tokens/tool_source/tool count, all in
        # one place so a spawn can be fully characterized from a single log
        # line without cross-referencing the (worktree-local, cleanup-prone)
        # manifest JSON file.
        _max_tokens_val = manifest.get("max_tokens")
        _max_tokens_str = "default:200000" if _max_tokens_val is None else str(_max_tokens_val)
        logger.info(
            "openai_agents spawn_manifest_summary session=%s: model=%s, base_url=%s, "
            "credential env var=%s, max_tokens=%s, tool_source=%s, tool_count=%d",
            session_id,
            manifest.get("model"),
            manifest.get("base_url") or "<default>",
            key_env_name,
            _max_tokens_str,
            manifest["tool_source"],
            len(manifest["tools"]),
        )

        # [DEEPSEEK-DEBUG] Unconditional diagnostic for the
        # deepseek/deepseek-chat-via-OpenRouter empty-completion / malformed
        # tool-call investigation (2026-07-03). Confirms at the SPAWN side
        # (before the runner subprocess even starts) that: (1) the model
        # string is forwarded to the runner byte-for-byte with no name
        # mapping/rewriting anywhere in this adapter, and (2) bernstein never
        # sets any OpenRouter-recommended extra headers (HTTP-Referer,
        # X-Title) anywhere in ``_build_manifest``/``spawn`` - grep this repo's
        # ``src/bernstein/adapters/openai_agents*.py`` for "extra_headers" /
        # "HTTP-Referer" / "X-Title" to confirm; there are none. If OpenRouter
        # routing/model behavior depends on those headers being present, this
        # is the log line proving they are absent for this spawn.
        logger.info(
            "[DEEPSEEK-DEBUG] spawn session=%s: model string forwarded verbatim (no "
            "name mapping) model=%r, base_url=%r, credential env var=%r (value never logged), "
            "extra_headers_configured=False (bernstein sets no HTTP-Referer/X-Title/"
            "any custom header anywhere in this adapter or the runner)",
            session_id,
            manifest.get("model"),
            manifest.get("base_url") or "<default>",
            key_env_name,
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

        env_keys = list(_OPENAI_CREDENTIAL_KEYS)
        if key_env_name not in env_keys:
            # Pass the override key through the filtered env so the runner
            # can resolve it by name.
            env_keys.append(key_env_name)
        env = build_filtered_env(env_keys)
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
