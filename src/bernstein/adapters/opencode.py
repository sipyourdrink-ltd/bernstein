"""OpenCode CLI adapter.

Last verified against upstream OpenCode (anomalyco/opencode) 1.18.16 on 2026-08-12.
Install: ``curl -fsSL https://opencode.ai/install | bash`` (fastest),
``brew install anomalyco/tap/opencode``, or ``npm i -g opencode-ai@latest``.

Permission posture is pinned by this adapter rather than inherited. OpenCode
resolves tool permissions from the operator's own config unless something
overrides it, so an un-pinned spawn means two operators running the same plan
get different agent behaviour. Every spawn therefore carries an explicit
``OPENCODE_PERMISSION`` policy derived from the adapter's declared
:class:`~bernstein.adapters._contract.DangerousModeStrategy`, which is what
keeps that declaration load-bearing instead of descriptive.

Neither policy resolves to ``ask``: a headless run whose permission resolves
that way waits forever (upstream ``anomalyco/opencode#36762``), and the only
backstop would be the timeout watchdog, which reports a timeout rather than a
blocked permission.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import DangerousModeStrategy
from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_OPENCODE_AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"

#: Env var carrying the permission policy. Preferred over the escalating CLI
#: flags for the *floor* because it is the only surface that can express the
#: restricted direction too, so one mechanism pins both.
_PERMISSION_ENV = "OPENCODE_PERMISSION"

#: The CLI flag named by ``DangerousModeStrategy.CLI_FLAG`` for this adapter.
#: Passed alongside the env policy so the escalation is visible in the spawn
#: record rather than only in the process environment.
_ESCALATED_PERMISSION_FLAG = "--auto"

#: Tool permissions for a run that is allowed to act unattended.
_ESCALATED_PERMISSION: dict[str, str] = {"edit": "allow", "bash": "allow", "webfetch": "allow"}

#: Tool permissions for a run that is not. ``deny`` rather than ``ask`` so the
#: run fails fast and legibly instead of blocking on a prompt nobody answers.
_RESTRICTED_PERMISSION: dict[str, str] = {"edit": "deny", "bash": "deny", "webfetch": "deny"}


class OpenCodeAdapter(CLIAdapter):
    """Spawn and monitor OpenCode CLI sessions."""

    #: OpenCode exposes ``--continue`` to re-enter the prior session, which is
    #: the flag behind the ``resume`` axis this adapter declares. The retry
    #: surface derives a warm continuation from that declaration
    #: (``checkpoint_retry_capability``), so the opt-in and the declaration
    #: have to move together.
    supports_session_continuation = True

    def _dangerous_mode(self) -> DangerousModeStrategy:
        """Return the declared dangerous-mode strategy for this adapter."""
        declared = getattr(self.strategy(), "dangerous_mode", DangerousModeStrategy.UNSUPPORTED)
        return declared if isinstance(declared, DangerousModeStrategy) else DangerousModeStrategy.UNSUPPORTED

    def _permission_escalated(self) -> bool:
        """Whether this spawn may skip interactive tool-permission prompts."""
        return self._dangerous_mode() in (DangerousModeStrategy.CLI_FLAG, DangerousModeStrategy.ALWAYS_ON)

    def _build_command(
        self,
        *,
        model: str,
        prompt: str,
        continuation_args: Sequence[str] = (),
    ) -> list[str]:
        """Build the ``opencode run`` argv for one spawn.

        Args:
            model: Model identifier passed to ``-m``.
            prompt: The task prompt; stays positional and last.
            continuation_args: Flags that re-enter a prior session, as
                returned by :meth:`continuation_args`. Empty for a fresh
                spawn, so a first run never claims to continue anything.

        Returns:
            The full argv, permission flag included when the declared
            dangerous-mode strategy escalates.
        """
        cmd = ["opencode", "run", "-m", model, "--format", "json"]
        if self._permission_escalated():
            cmd.append(_ESCALATED_PERMISSION_FLAG)
        cmd.extend(continuation_args)
        cmd.append(prompt)
        return cmd

    def _permission_env(self) -> dict[str, str]:
        """Return the explicit permission policy for the spawn environment.

        Set on every spawn in both directions. Passing only the escalating
        CLI flag would leave the restricted case resolving against the host's
        own config, which is the behaviour this pins shut.
        """
        policy = _ESCALATED_PERMISSION if self._permission_escalated() else _RESTRICTED_PERMISSION
        return {_PERMISSION_ENV: json.dumps(policy, sort_keys=True)}

    def continuation_args(self, _session_id: str) -> list[str]:
        """Return the flags that re-enter this adapter's prior session.

        OpenCode's ``--continue`` re-enters the most recent session for the
        working directory. Bernstein runs every task in its own git worktree,
        so "most recent session here" resolves to this task's own session
        without explicit id plumbing.
        """
        return ["--continue"]

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

        if not _has_opencode_auth():
            logger.warning(
                "OpenCodeAdapter: no OpenCode/provider auth detected - spawn may fail until "
                "`opencode auth login` or provider env vars are configured"
            )
        if mcp_config:
            logger.debug("OpenCodeAdapter ignoring runtime MCP config injection for session %s", session_id)

        cmd = self._build_command(model=model_config.model, prompt=prompt)

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

        env = build_filtered_env(
            [
                "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_DIR",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "OPENROUTER_API_KEY",
                "OPENROUTER_API_KEY_PAID",
                "XAI_API_KEY",
                "GITLAB_TOKEN",
            ]
        )
        # Applied after the allow-list so the pinned policy is what the worker
        # sees, whatever the operator's own environment or config file holds.
        env.update(self._permission_env())
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
                raise RuntimeError("opencode not found in PATH. Install it from https://opencode.ai/docs/cli/") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing opencode: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="opencode")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "OpenCode"

    def detect_tier(self) -> ApiTierInfo | None:
        """Best-effort OpenCode tier detection from auth state."""
        if not _has_opencode_auth():
            return None

        return ApiTierInfo(
            provider=ProviderType.OPENCODE,
            tier=ApiTier.PRO,
            rate_limit=RateLimit(requests_per_minute=120, tokens_per_minute=40_000),
            is_active=True,
        )


def _has_opencode_auth() -> bool:
    """Return True when OpenCode has a credentials file or provider API key."""
    if _OPENCODE_AUTH_FILE.exists():
        return True
    key_vars = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY_PAID",
        "XAI_API_KEY",
        "GITLAB_TOKEN",
    )
    return any(os.environ.get(name) for name in key_vars)
