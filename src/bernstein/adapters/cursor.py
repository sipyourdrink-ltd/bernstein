"""Cursor Agent CLI adapter.

Last verified against upstream ``cursor-agent`` 2026.05.x on 2026-05-05.

Cursor publicly launched a real terminal CLI on 2026-01-16 and shipped a v3
"agent-first" UX in April plus the Cursor SDK in May 2026, all sharing the same
``cursor-agent`` binary surface.

Install: ``curl https://cursor.com/install -fsS | bash`` (macOS/Linux/WSL).
The binary is ``cursor-agent``; the docs frequently abbreviate it as ``agent``
in examples, but the executable on PATH is the full hyphenated name.

Auth: ``cursor-agent login`` opens a browser for OAuth; for CI use
``CURSOR_API_KEY`` env var.

Headless invocation surface (per https://cursor.com/docs/cli):

* ``-p / --print``       - non-interactive one-shot mode (without this the
                           CLI starts a TTY chat and never returns).
* ``--workspace <path>`` - project root the agent operates against.
* ``--model <name>``     - e.g. ``claude-sonnet-4-6``, ``claude-opus-4``,
                           ``gpt-5.2``.
* ``--output-format stream-json``
                         - emits JSON deltas suitable for tailing into the
                           runtime log.
* ``--trust``            - skip the workspace-trust prompt (required for
                           first-run headless).
* ``--approve-mcps``     - auto-approve configured MCP servers.
* ``-f / --force``       - actually edit files in print mode; without it
                           the CLI only prints suggestions (silent no-op).
                           Suppressed when ``task_scope == "readonly"``,
                           which switches the run to ``--mode ask``.
* ``--mode ask``         - read-only, no edits, no tools that mutate state.

MCP: there is no ``--add-mcp`` flag.  The CLI shares the editor's
``.cursor/mcp.json`` config (project precedence).  When ``mcp_config`` is
supplied we write it to ``<workdir>/.cursor/mcp.json`` before spawn.

Prompt: written to a temp file in the workdir and fed to ``cursor-agent``
via stdin redirection rather than as a positional argument.  This avoids
shell escaping pitfalls for multi-line prompts and keeps the argv short
in process listings.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import AdapterCapability, AdapterPluginInfo
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit


class CursorAdapter(CLIAdapter):
    """Spawn and monitor Cursor Agent CLI sessions."""

    # Cursor surfaces 429s when its proxy back-pressures the upstream
    # model account; we record under the proxy label so the panel does
    # not falsely attribute the hit to the underlying model vendor.
    rate_limit_provider = "cursor"

    def plugin_info(self) -> AdapterPluginInfo:
        """Declare built-in Cursor adapter capabilities."""
        return AdapterPluginInfo(
            name="cursor",
            version="builtin",
            capabilities=(AdapterCapability.SUPPORTS_REMOTE_MCP,),
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
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # MCP config: write to .cursor/mcp.json (the CLI shares the editor's
        # config; there is no --add-mcp flag).  This must happen before
        # spawn so cursor-agent sees the file when it boots.
        if mcp_config:
            cursor_dir = workdir / ".cursor"
            cursor_dir.mkdir(parents=True, exist_ok=True)
            (cursor_dir / "mcp.json").write_text(
                json.dumps(mcp_config, indent=2),
                encoding="utf-8",
            )

        # Prompt → temp file in the runtime dir, then stdin-redirected into
        # cursor-agent.  We keep it under .sdd/runtime so cleanup tooling
        # already prunes it; naming by session_id makes it diagnosable.
        prompt_dir = workdir / ".sdd" / "runtime" / "cursor"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{session_id}.prompt"
        prompt_path.write_text(prompt, encoding="utf-8")

        # Build the inner cursor-agent argv.
        cmd: list[str] = [
            "cursor-agent",
            "-p",
            "--workspace",
            str(workdir),
            "--output-format",
            "stream-json",
            "--trust",
            "--approve-mcps",
        ]

        # Pass model when supplied; cursor-agent accepts the same names the
        # editor uses (claude-sonnet-4-6, claude-opus-4, gpt-5.2, ...).
        if model_config.model:
            cmd += ["--model", model_config.model]

        # Read-only tasks run in ask mode (no edits, no mutating tools);
        # everything else needs --force to actually apply changes in print
        # mode.  Without --force, cursor-agent only suggests diffs, which
        # for an orchestrated agent is a silent no-op.
        if task_scope == "readonly":
            cmd += ["--mode", "ask"]
        else:
            cmd += ["--force"]

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

        # CURSOR_API_KEY for headless CI auth; OAuth via ~/.cursor/ still
        # works if the env var is unset and the user has logged in locally.
        env = build_filtered_env(["CURSOR_API_KEY"])

        with log_path.open("w") as log_file, prompt_path.open("rb") as prompt_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdin=prompt_file,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "cursor-agent not found in PATH. Install: curl https://cursor.com/install -fsS | bash"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing cursor-agent: {exc}") from exc

        # Detect fast-exit rate-limit signals before declaring the spawn
        # successful; this also records on the per-adapter meter via the
        # base-class hook.
        self._probe_fast_exit(proc, log_path, provider_name="cursor")

        # ``proc`` MUST be threaded into ``SpawnResult``; downstream
        # spawner_core uses it for stdin-IPC registration and ``bernstein
        # adapter run`` calls ``result.proc.wait(...)`` to detect early
        # exits.  Without it the orchestrator can't observe completion
        # and zombie pids accumulate until the timeout watchdog fires.
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Cursor"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Cursor subscription tier.

        Cursor subscription tiers:
        - Free: 50 slow requests/month
        - Pro: $20/mo, 500 fast requests + unlimited slow
        - Business: $40/mo, unlimited fast requests

        Cursor does not expose subscription tier via the CLI, so we treat
        the presence of either ``~/.cursor/`` (OAuth login) or the
        ``CURSOR_API_KEY`` env var (CI auth) as a proxy for being
        authenticated and report PRO as a conservative estimate.

        Returns:
            ApiTierInfo with detected tier and rate limits, or None if no
            credentials are available.
        """
        import os
        from pathlib import Path

        cursor_dir = Path.home() / ".cursor"
        has_oauth = cursor_dir.exists()
        has_api_key = bool(os.environ.get("CURSOR_API_KEY"))
        if not (has_oauth or has_api_key):
            return None

        # Cursor Pro is the most common paid tier - conservative estimate.
        tier = ApiTier.PRO
        rate_limit = RateLimit(
            requests_per_minute=50,  # 500 fast req/month ≈ ~50/min burst
            tokens_per_minute=20_000,
        )

        return ApiTierInfo(
            provider=ProviderType.CURSOR,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
