"""OpenAI Codex CLI adapter.

Last verified against upstream @openai/codex 0.152.1 on 2026-09-02.
Install: ``npm i -g @openai/codex`` (or ``brew install --cask codex``).
Recommended models: ``gpt-5.5`` (GA 2026-04-24), which is also the pinned
fallback, or ``gpt-5.4-mini`` for cheap work.  ``gpt-5.4`` is no longer served
on the ChatGPT-account auth path.  The o-series reasoning models (``o3``,
``o4-mini``) are also accepted by the CLI.

Sandbox posture is derived from the adapter's declared
:class:`~bernstein.adapters._contract.DangerousModeStrategy` rather than
hardcoded, because the right answer depends on where the CLI runs.

``codex exec --sandbox workspace-write`` is implemented with bubblewrap on
Linux, and bubblewrap needs an unprivileged user namespace to start. A runner
that already provides isolation typically denies exactly that: a container
started with ``--cap-drop ALL --security-opt no-new-privileges:true``, or a
host with unprivileged user namespaces disabled, makes every model-issued
shell command fail with ``bwrap: No permissions to create a new namespace``.
The failure is silent from the orchestrator's side -- ``codex exec`` still
emits ``turn.completed`` and exits 0 after producing an empty diff -- so the
run reads as a model that had nothing to do rather than as a sandbox that
could not initialise.

An operator whose runner is already isolated therefore declares the escalated
strategy, and the spawn passes ``--dangerously-bypass-approvals-and-sandbox``
instead. Upstream's own help text scopes that flag the same way: "Intended
solely for running in environments that are externally sandboxed." The
un-escalated default stays ``--sandbox workspace-write``, so a spawn on a
plain host keeps the vendor sandbox.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters._contract import DangerousModeStrategy
from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit
from bernstein.core.platform_compat import process_group_popen_kwargs

logger = logging.getLogger(__name__)

# Codex authenticates via either OPENAI_API_KEY or a ChatGPT OAuth session that
# ``codex login`` stores in ~/.codex/auth.json. ~/.codex is already the canonical
# Codex config dir (see agent_discovery and preflight), so its auth.json sibling
# is the right signal for "an OAuth session exists".
_CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# Claude cascade tier names are not valid Codex model identifiers. If an upstream
# selector hands one to this adapter (e.g. the high-stakes-role default), fall
# back to a Codex model so ``codex exec -m`` receives something the CLI accepts.
# The real selection fix lives in the spawner; this is a last-resort safety net.
#
# ``gpt-5.4`` was the pin until 2026-09-02 and no longer works on the
# ChatGPT-account auth path: the backend rejects it with HTTP 400
# ``invalid_request_error`` -- "The 'gpt-5.4' model is not supported when using
# Codex with a ChatGPT account" -- and the account's own model catalogue lists
# only ``gpt-5.5`` and ``gpt-5.4-mini``. A last-resort fallback that 400s is
# worse than no fallback, so the pin follows the recommended GA model, which
# both auth paths accept.
_DEFAULT_CODEX_MODEL = "gpt-5.5"
_CLAUDE_TIER_MODELS = frozenset({"opus", "sonnet", "haiku"})

#: Sandbox argv for a spawn that keeps the vendor sandbox. Codex implements
#: this profile with bubblewrap on Linux, so it needs an unprivileged user
#: namespace the host must allow.
_SANDBOXED_ARGS: tuple[str, ...] = ("--sandbox", "workspace-write")

#: Sandbox argv for a spawn whose runner already provides isolation. Upstream
#: scopes the flag to exactly that case: "Intended solely for running in
#: environments that are externally sandboxed."
_BYPASS_SANDBOX_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def _has_codex_auth() -> bool:
    """Return True when Codex has a usable credential: an API key or OAuth session."""
    return bool(os.environ.get("OPENAI_API_KEY")) or _CODEX_AUTH_FILE.exists()


def _codex_model(model: str) -> str:
    """Map a Claude cascade tier name to the Codex default; pass any other model through."""
    if model in _CLAUDE_TIER_MODELS:
        logger.warning(
            "CodexAdapter: model %r is a Claude tier name Codex cannot run; using %r "
            "instead. Set role_model_policy.<role>.model or default_model to a Codex "
            "model (e.g. gpt-5.5) to choose explicitly.",
            model,
            _DEFAULT_CODEX_MODEL,
        )
        return _DEFAULT_CODEX_MODEL
    return model


class CodexAdapter(CLIAdapter):
    """Spawn and monitor OpenAI Codex CLI sessions."""

    registry_name = "codex"
    # Provider-string aliases this adapter resolves from in
    # ``_infer_adapter_name_for_provider``. NOTE: "openai" and "gpt" are
    # broad aliases that historically also matched the openai_agents
    # provider string via substring search (see 042bcbd0). The registry
    # requires exact provider-name matches, so this alias set only ever
    # matches a provider literally named "codex", "openai", or "gpt" --
    # it can no longer swallow "openai_agents".
    provides = ("codex", "openai", "gpt")
    # Default model when no operator-pinned model reaches this adapter. Read by
    # the spawner to substitute Claude tier names for non-Claude adapters.
    default_model = _DEFAULT_CODEX_MODEL
    external_endpoints = (("api.openai.com", 443),)
    # OpenAI returns HTTP 429 with ``rate_limit_exceeded`` /
    # ``insufficient_quota`` error codes; the meter records both under
    # the same provider label.
    rate_limit_provider = "openai"

    def _dangerous_mode(self) -> DangerousModeStrategy:
        """Return the declared dangerous-mode strategy for this adapter."""
        declared = getattr(self.strategy(), "dangerous_mode", DangerousModeStrategy.UNSUPPORTED)
        return declared if isinstance(declared, DangerousModeStrategy) else DangerousModeStrategy.UNSUPPORTED

    def _sandbox_bypassed(self) -> bool:
        """Whether this spawn runs Codex without its own sandbox.

        Only :attr:`DangerousModeStrategy.ALWAYS_ON` bypasses. That value
        means "no permission surface exists to skip", which is what the
        bypass flag produces: no approval prompt and no vendor sandbox. The
        shipped declaration for this adapter is
        :attr:`DangerousModeStrategy.CLI_FLAG` -- a flag pins the posture,
        and the posture it pins is the sandboxed one -- so a default spawn
        keeps ``--sandbox workspace-write``. An operator whose runner is
        already isolated declares ``ALWAYS_ON`` instead.
        """
        return self._dangerous_mode() is DangerousModeStrategy.ALWAYS_ON

    def _sandbox_args(self) -> tuple[str, ...]:
        """Return the sandbox argv for one spawn, derived from the declaration."""
        if not self._sandbox_bypassed():
            return _SANDBOXED_ARGS
        logger.warning(
            "CodexAdapter: dangerous_mode=%s, so this spawn passes %s -- model-issued "
            "shell commands run with no Codex sandbox. Declare this only when the "
            "runner itself provides the isolation.",
            DangerousModeStrategy.ALWAYS_ON,
            _BYPASS_SANDBOX_FLAG,
        )
        return (_BYPASS_SANDBOX_FLAG,)

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
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = workdir / ".sdd" / "runtime" / f"{session_id}.last-message.txt"

        if not _has_codex_auth():
            logger.warning(
                "CodexAdapter: no OPENAI_API_KEY and no Codex OAuth session "
                "(~/.codex/auth.json) detected - spawn may fail until `codex login` is "
                "run or OPENAI_API_KEY is set",
            )

        model = _codex_model(model_config.model)
        cmd = [
            "codex",
            "exec",
            *self._sandbox_args(),
            "-m",
            model,
            "--json",
            "-o",
            str(output_path),
        ]
        # Session-id binding is contract-driven: the argv gains a flag only
        # when the contract names one. ``codex exec`` exposes no flag that
        # accepts a caller-supplied session id -- only a ``resume
        # <SESSION_ID>`` subcommand, which reattaches to an existing session
        # and cannot bind one at spawn time -- so the codex contract names no
        # flag and this stays an empty list (issue #4135). The derived id is
        # still recorded in orchestrator state for cross-reference.
        cmd.extend(self.session_id_args(session_id))
        cmd.append(prompt)

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model,
        )

        env = build_filtered_env(["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_BASE_URL"])
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    **process_group_popen_kwargs(),
                )
            except FileNotFoundError as exc:
                raise RuntimeError("codex not found in PATH. Install it with: npm install -g @openai/codex") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing codex: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="codex")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Codex"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Codex API tier based on environment configuration.

        Checks OPENAI_API_KEY and OPENAI_ORG_ID to determine tier:
        - With organization ID = Enterprise tier
        - With paid account (sk-proj...) = Pro tier
        - Default = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "")
        org_id = os.environ.get("OPENAI_ORG_ID", "")

        if not api_key:
            return None

        # Determine tier from environment and key format
        if org_id:
            tier = ApiTier.ENTERPRISE
            rate_limit = RateLimit(
                requests_per_minute=500,
                tokens_per_minute=90000,
            )
        elif api_key.startswith("sk-proj"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        elif api_key.startswith("sk-"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(
                requests_per_minute=60,
                tokens_per_minute=5000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            )

        return ApiTierInfo(
            provider=ProviderType.CODEX,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
