"""Claude Code CLI adapter."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Mapping  # noqa: TC003 — runtime use in ClassVar annotations
from pathlib import Path
from typing import Any, ClassVar, cast

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.claude_agents import build_agents_json
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.defaults import COST
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit
from bernstein.core.platform_compat import kill_process_group_graceful, process_alive

# Map short model names to Claude Code CLI model IDs.
# Updated 2026-04-16 — Opus 4.7 generally available, same price as 4.6.
_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "opus-4-6": "claude-opus-4-6",  # pinned fallback
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# JSON schema for structured agent output — enforced via --json-schema so
# the result is always machine-parseable by the orchestrator.
_RESULT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "failed", "partial"]},
            "summary": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "exit_reason": {"type": "string"},
        },
        "required": ["status", "summary"],
    }
)


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


def load_mcp_config(
    project_servers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build merged MCP config from user global config and project-level overrides.

    Reads ~/.claude/mcp.json (user's global MCP servers), then merges in any
    project-level mcp_servers from bernstein.yaml. Project config wins on conflicts.

    Args:
        project_servers: MCP server definitions from bernstein.yaml mcp_servers field.

    Returns:
        Merged MCP config dict ready for --mcp-config, or None if no servers found.
    """
    merged: dict[str, Any] = {}

    # 1. Read user global config (~/.claude/mcp.json)
    global_path = Path.home() / ".claude" / "mcp.json"
    if global_path.exists():
        try:
            global_cfg = json.loads(global_path.read_text(encoding="utf-8"))
            if isinstance(global_cfg, dict):
                # mcp.json has {"mcpServers": {...}} structure
                cfg = cast(_CAST_DICT_STR_ANY, global_cfg)
                servers = cfg.get("mcpServers", cfg)
                if isinstance(servers, dict):
                    merged.update(cast(_CAST_DICT_STR_ANY, servers))
        except (OSError, json.JSONDecodeError):
            pass  # Global MCP config unreadable; skip

    # 2. Merge project-level config (overrides global)
    if project_servers:
        # Expand env vars in server config values
        for name, server_def in project_servers.items():
            resolved = _resolve_env_vars(server_def)
            merged[name] = resolved

    if not merged:
        return None

    return {"mcpServers": merged}


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively resolve ${VAR} references in config values."""
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        var_name = obj[2:-1]
        return os.environ.get(var_name, obj)
    if isinstance(obj, dict):
        d = cast(_CAST_DICT_STR_ANY, obj)
        return {k: _resolve_env_vars(v) for k, v in d.items()}
    if isinstance(obj, list):
        lst = cast("list[Any]", obj)
        return [_resolve_env_vars(item) for item in lst]
    return obj


_logger = logging.getLogger(__name__)


def build_cacheable_system_blocks(
    system_addendum: str,
) -> list[dict[str, Any]]:
    """Build Anthropic API system message blocks with cache control hints.

    Wraps the static system addendum (role template + coding standards) in
    a content block with ``cache_control: {"type": "ephemeral"}``.  When
    used with the Anthropic Messages API, this instructs the provider to
    cache the block for up to 5 minutes, reducing input token costs for
    repeated spawns with the same role.

    The Claude Code CLI handles caching transparently when content is
    passed via ``--append-system-prompt``.  This function is provided for
    adapters that call the API directly or for future Claude Code CLI
    versions that support explicit cache control.

    Args:
        system_addendum: Static system prompt content to mark as cacheable.

    Returns:
        List of Anthropic API content blocks.  If *system_addendum* is
        non-empty, the block includes ``cache_control``.  Returns an
        empty list if the addendum is empty.
    """
    if not system_addendum:
        return []
    return [
        {
            "type": "text",
            "text": system_addendum,
            "cache_control": {"type": "ephemeral"},
        }
    ]


# How long a cached rate-limit probe result stays valid (seconds).
_RATE_LIMIT_CACHE_TTL: float = COST.rate_limit_cache_ttl_s

# Cooldown applied when rate-limiting is detected (seconds).
_RATE_LIMIT_COOLDOWN: float = COST.rate_limit_cooldown_s


class ClaudeCodeAdapter(CLIAdapter):
    """Spawn and monitor Claude Code CLI sessions."""

    # Track Popen objects for reliable is_alive() via poll()
    _procs: ClassVar[dict[int, subprocess.Popen[bytes]]] = {}
    _wrapper_pids: ClassVar[dict[int, int]] = {}  # claude_pid → wrapper_pid

    # Tool allowlists by role — agents only get the tools they need.
    # Reduces attack surface and prevents agents from using tools outside
    # their scope (e.g. qa agent shouldn't use Write to create new files).
    _ROLE_ALLOWED_TOOLS: ClassVar[dict[str, str]] = {
        "qa": "Bash Read Grep Glob Agent",
        "reviewer": "Bash Read Grep Glob",
        "docs": "Read Write Edit Grep Glob",
        "security": "Bash Read Grep Glob",
    }

    def __init__(self) -> None:
        super().__init__()
        # Timestamp until which the provider is assumed rate-limited.
        self._rate_limit_until: float = 0.0
        # Timestamp of last successful (non-rate-limited) probe, for caching.
        self._rate_limit_checked_at: float = 0.0

    def is_rate_limited(self) -> bool:
        """Probe ``claude --version`` to detect provider-side rate limiting.

        Returns a cached result for ``_RATE_LIMIT_CACHE_TTL`` seconds to avoid
        spamming the CLI.  When rate limiting is detected, sets a cooldown of
        ``_RATE_LIMIT_COOLDOWN`` seconds during which all spawns are skipped.
        """
        now = time.time()

        # Active cooldown — provider is known rate-limited.
        if now < self._rate_limit_until:
            return True

        # Cached negative result — recently checked and was fine.
        if now - self._rate_limit_checked_at < _RATE_LIMIT_CACHE_TTL:
            return False

        # Probe with a real API call — `claude --version` doesn't hit the API
        # and can't detect account-level rate limits like "You've hit your limit".
        try:
            result = subprocess.run(
                ["claude", "--print", "--max-turns", "1", "--output-format", "text", "-p", "say ok"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COST.rate_limit_probe_timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _logger.debug("Rate-limit probe failed: %s", exc)
            self._rate_limit_checked_at = now
            return False

        combined = (result.stdout + result.stderr).lower()
        if "hit your limit" in combined or "rate limit" in combined or "resets" in combined:
            self._rate_limit_until = now + _RATE_LIMIT_COOLDOWN
            _logger.warning(
                "Claude Code rate-limited; blocking spawns for %.0fs",
                _RATE_LIMIT_COOLDOWN,
            )
            return True

        self._rate_limit_checked_at = now
        return False

    # Max turns used for batch-mode agents, which must cover research +
    # decompose + spawn workers + track completion.
    BATCH_MAX_TURNS: int = COST.batch_max_turns

    # Scope → base budget mapping.  Opus tasks get a 2x multiplier because
    # opus input/output tokens cost roughly twice as much as sonnet.
    # Widened to ``Mapping`` because ``defaults.COST`` returns a
    # ``MappingProxyType`` view (audit-155).
    _SCOPE_BUDGET_USD: ClassVar[Mapping[str, float]] = COST.scope_budget_usd

    # Scope multipliers: large tasks get proportionally more turns so they
    # don't die prematurely.
    _SCOPE_MULTIPLIERS: ClassVar[Mapping[str, float]] = COST.scope_multipliers

    def _build_command(
        self,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        prompt: str,
        *,
        role: str = "",
        workdir: Path | None = None,
        agents_json: dict[str, Any] | None = None,
        system_addendum: str = "",
        batch_mode: bool = False,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
    ) -> list[str]:
        """Build the claude CLI command with effort mapping.

        Uses ``--permission-mode bypassPermissions`` instead of the deprecated
        ``--dangerously-skip-permissions`` flag, adds ``--fallback-model``
        for automatic failover, ``--allowedTools`` for role-scoped
        tool access, and ``--agents`` for per-task subagent definitions.

        Args:
            model_config: Model and effort configuration.
            mcp_config: MCP server definitions to inject.
            prompt: The task prompt.
            role: Agent role (used for tool allowlisting).
            workdir: Project working directory (used for CLAUDE.md context dirs).
            agents_json: Custom subagent definitions for ``--agents`` flag.
                When provided, Claude Code's Agent tool will use these
                definitions instead of generic defaults.
            system_addendum: Orchestration context to inject via
                ``--append-system-prompt``.  Keeps signal-check instructions,
                completion protocol, heartbeat commands, etc. out of the user
                prompt so the agent focuses on the task goal.
            batch_mode: When True, sets ``--max-turns`` to
                :attr:`BATCH_MAX_TURNS` (200) so the agent has enough turns
                to research, decompose, spawn workers, and track their
                completion via the ``/batch`` skill.
            task_scope: Task scope ("small", "medium", "large") used to
                compute a per-task budget cap.  Opus models get a 2x
                multiplier because their token costs are roughly double.
            budget_multiplier: Additional multiplier applied on top of the
                scope-based budget (e.g. 2.0 when retrying after hitting the
                budget cap in a previous attempt).
        """
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        effort = getattr(model_config, "effort", "high")
        base_turns = COST.effort_base_turns.get(effort, 50)
        scope_multiplier = self._SCOPE_MULTIPLIERS.get(task_scope, 1.5)
        max_turns = self.BATCH_MAX_TURNS if batch_mode else int(base_turns * scope_multiplier)
        effort_map = {"max": "max", "high": "high", "medium": "medium", "normal": "medium", "low": "low"}
        claude_effort = effort_map.get(effort, "high")

        # Choose fallback model: opus-4-7 → opus-4-6 → sonnet → haiku
        _fallback_map = {
            "claude-opus-4-7": "claude-opus-4-6",
            "claude-opus-4-6": "claude-sonnet-4-6",
            "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
        }
        fallback_model = _fallback_map.get(model_id)

        cmd = [
            "claude",
            "--model",
            model_id,
            "--effort",
            claude_effort,
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            str(max_turns),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--no-session-persistence",
        ]
        if fallback_model:
            cmd.extend(["--fallback-model", fallback_model])

        # Role-scoped tool allowlisting: restrict non-coding roles to read-only
        # tools to prevent unintended modifications.
        allowed_tools = self._ROLE_ALLOWED_TOOLS.get(role)
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])

        # Inject project CLAUDE.md context so the agent picks up coding standards,
        # architecture notes, and any task-specific instructions automatically.
        # --add-dir tells Claude Code to load CLAUDE.md from the given directory.
        if workdir is not None:
            claude_md = workdir / "CLAUDE.md"
            if claude_md.exists():
                cmd.extend(["--add-dir", str(workdir)])

        # Inject per-task subagent definitions so Claude Code's Agent tool
        # spawns role-scoped subagents instead of generic defaults.
        if agents_json:
            cmd.extend(["--agents", json.dumps(agents_json)])

        # Per-task budget cap — scope-aware to avoid killing large tasks mid-work.
        # Opus models get a 2x multiplier because their tokens cost ~2x more.
        # Retry budget_multiplier (e.g. 2.0 after budget-cap failure) stacks on top.
        base_budget = self._SCOPE_BUDGET_USD.get(task_scope, 5.0)
        is_opus = "opus" in model_id.lower()
        budget_usd = base_budget * (COST.opus_budget_multiplier if is_opus else 1.0) * budget_multiplier
        cmd.extend(["--max-budget-usd", f"{budget_usd:.2f}"])

        # Enforce structured output so the orchestrator can always parse results
        cmd.extend(["--json-schema", _RESULT_SCHEMA])

        if mcp_config:
            cmd.extend(["--mcp-config", json.dumps(mcp_config)])

        if system_addendum:
            cmd.extend(["--append-system-prompt", system_addendum])

        cmd.extend(["-p", prompt])
        return cmd

    @staticmethod
    def _wrapper_script(
        session_id: str = "",
        tokens_path: str = "",
        heartbeat_path: str = "",
        completion_path: str = "",
    ) -> str:
        """Return the stream-json → human-readable log converter script.

        Parses Claude Code's NDJSON stream, extracts human-readable text for
        the log file, writes token usage to a sidecar, touches a heartbeat
        file on every event so the orchestrator knows the agent is alive, and
        writes a completion marker when a ``result`` event is received so the
        orchestrator can reap the agent immediately instead of waiting for the
        heartbeat to go stale.

        Args:
            session_id: Agent session ID, injected for token sidecar writes.
            tokens_path: Absolute path to the ``.tokens`` sidecar file.
            heartbeat_path: Absolute path to the heartbeat file (touched on each event).
            completion_path: Absolute path to the completion marker file.  Written
                when a ``result`` event is parsed, signalling the orchestrator that
                the agent finished its work and can be reaped immediately.
        """
        token_writer = ""
        if tokens_path:
            token_writer = (
                "        usage = msg.get('usage') or {}\n"
                "        if not usage:\n"
                "            usage = msg.get('message', {}).get('usage') or {}\n"
                "        inp_tok = int(usage.get('input_tokens', 0))\n"
                "        out_tok = int(usage.get('output_tokens', 0))\n"
                "        if inp_tok or out_tok:\n"
                "            import time as _t\n"
                f"            _rec = json.dumps({{'ts': _t.time(), 'in': inp_tok, 'out': out_tok}})\n"
                f"            try:\n"
                f"                with open({tokens_path!r}, 'a') as _tf:\n"
                f"                    _tf.write(_rec + '\\n')\n"
                f"            except OSError:\n"
                f"                pass\n"
            )
        # Heartbeat: touch the heartbeat file on every parsed JSON event.
        # This gives the orchestrator a reliable, real-time liveness signal
        # instead of relying on log file mtime which may buffer.
        heartbeat_touch = ""
        if heartbeat_path:
            heartbeat_touch = (
                "    # Touch heartbeat file on every event\n"
                "    try:\n"
                "        _hb = {'timestamp': __import__('time').time(), 'phase': 'implementing',"
                " 'progress_pct': 0, 'current_file': '', 'message': 'working', 'status': 'working'}\n"
                f"        with open({heartbeat_path!r}, 'w') as _hf:\n"
                f"            _hf.write(__import__('json').dumps(_hb))\n"
                "    except OSError:\n"
                "        pass\n"
            )
        # Completion marker: write a file when the agent emits a `result` event
        # so the orchestrator can reap the slot immediately instead of waiting
        # for the heartbeat to go stale (saves up to 300s per agent).
        completion_write = ""
        if completion_path:
            completion_write = (
                "        try:\n"
                "            import json as _json\n"
                "            _marker = _json.dumps({'result': txt or '', 'subtype': _subtype,"
                " 'cost_usd': _cost, 'turns': _turns, 'duration_ms': _dur})\n"
                f"            with open({completion_path!r}, 'w') as _cf:\n"
                f"                _cf.write(_marker)\n"
                "        except OSError:\n"
                "            pass\n"
            )
        return (
            "import sys, json\n"
            "seen_text = set()\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    try:\n"
            "        msg = json.loads(raw)\n"
            "    except json.JSONDecodeError:\n"
            "        continue\n" + heartbeat_touch + "    t = msg.get('type', '')\n"
            "    if t == 'assistant':\n"
            "        for block in msg.get('message', {}).get('content', []):\n"
            "            if block.get('type') == 'text':\n"
            "                txt = block['text']\n"
            "                if txt not in seen_text:\n"
            "                    seen_text.add(txt)\n"
            "                    print(txt, flush=True)\n"
            "            elif block.get('type') == 'tool_use':\n"
            "                name = block.get('name', '?')\n"
            "                inp = str(block.get('input', ''))[:150]\n"
            "                print(f'[{name}] {inp}', flush=True)\n"
            "    elif t == 'result':\n"
            "        txt = msg.get('result', '')\n"
            "        if txt:\n"
            "            print(txt, flush=True)\n"
            "        # Extract structured result data for orchestrator\n"
            "        _subtype = msg.get('subtype', 'success')\n"
            "        _cost = msg.get('total_cost_usd', 0.0)\n"
            "        _turns = msg.get('num_turns', 0)\n"
            "        _dur = msg.get('duration_ms', 0)\n"
            "        print(f'[RESULT] subtype={_subtype} cost=${_cost:.4f}'"
            "              f' turns={_turns} duration={_dur}ms', flush=True)\n" + completion_write + token_writer
        )

    def _launch_process(
        self,
        cmd: list[str],
        wrapper: str,
        workdir: Path,
        log_path: Path,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]:
        """Launch claude piped through wrapper, writing output to log_path.

        The parent closes its copies of the log/stderr file handles after
        the subprocesses have inherited them.  On failure the handles are
        closed in the ``except`` path so they are never leaked.

        Args:
            cmd: The CLI command to execute (wrapped by bernstein-worker).
            wrapper: Python source for the stream-json log converter script.
            workdir: Working directory for both processes.
            log_path: Path where the wrapper writes decoded output.
            env: Filtered environment dict.  When provided, both child
                processes receive only these variables; when None the full
                parent environment is inherited (legacy behaviour).
        """
        log_file = log_path.open("w")
        stderr_path = log_path.with_suffix(".stderr.log")
        stderr_file = stderr_path.open("w")
        preexec_fn = self._get_preexec_fn()
        try:
            try:
                claude_proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("claude not found in PATH. Install Claude Code: https://claude.ai/code") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing claude: {exc}") from exc

            try:
                wrapper_proc = subprocess.Popen(
                    [sys.executable, "-c", wrapper],
                    stdin=claude_proc.stdout,
                    stdout=log_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    cwd=workdir,
                    env=env,
                )
            except Exception:
                claude_proc.kill()
                raise
        except Exception:
            # Subprocesses never started (or only partially); close handles
            # so they don't leak.
            log_file.close()
            stderr_file.close()
            raise

        # Both subprocesses are running and own the inherited FDs.
        # Close the parent's copies so the FDs aren't kept alive
        # longer than necessary, but the children can still write.
        log_file.close()
        stderr_file.close()

        # Allow claude_proc to receive SIGPIPE if wrapper dies
        if claude_proc.stdout:
            claude_proc.stdout.close()

        return claude_proc, wrapper_proc

    @staticmethod
    def _inject_hooks_config(
        workdir: Path,
        session_id: str,
        server_url: str = "http://127.0.0.1:8052",
    ) -> None:
        """Write Claude Code hooks config to ``.claude/settings.local.json``.

        Injects HTTP hooks for PostToolUse, Stop, PreCompact, SubagentStart,
        and SubagentStop events.  Each hook POSTs to the Bernstein task server
        so the orchestrator gets real-time visibility into agent activity.

        Each hook request is signed with HMAC-SHA256 over the raw body, using
        the shared secret in ``BERNSTEIN_HOOK_SECRET`` (or
        ``BERNSTEIN_AUTH_TOKEN``).  The server rejects unsigned requests.

        If the settings file already exists, the hooks key is merged in
        (preserving any other settings the user may have configured).

        Args:
            workdir: Project working directory (worktree root).
            session_id: Agent session identifier, embedded in the hook URL.
            server_url: Task server base URL (default localhost:8052).
        """
        settings_dir = workdir / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.local.json"

        hook_url = f"{server_url}/hooks/{session_id}"
        # Sign the body with HMAC-SHA256 before posting.  ``openssl`` is in the
        # base image on every supported OS; we read the secret from the env
        # var the adapter also exports when spawning the agent.
        curl_cmd = (
            "sh -c '"
            'SECRET="${BERNSTEIN_HOOK_SECRET:-$BERNSTEIN_AUTH_TOKEN}"; '
            "BODY=$(cat); "
            'SIG=$(printf "%s" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk "{print \\$2}"); '
            'curl -sS -X POST -H "Content-Type: application/json" '
            f'-H "X-Bernstein-Hook-Signature-256: sha256=$SIG" -d "$BODY" {hook_url}'
            "'"
        )
        hook_entry = {"type": "command", "command": curl_cmd}

        hook_events = ["PostToolUse", "Stop", "PreCompact", "SubagentStart", "SubagentStop"]
        hooks_config: dict[str, list[dict[str, Any]]] = {}
        for event_name in hook_events:
            hooks_config[event_name] = [
                {"matcher": "", "hooks": [hook_entry]},
            ]

        # Merge with existing settings if present
        existing: dict[str, Any] = {}
        if settings_path.exists():
            try:
                raw = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = cast(_CAST_DICT_STR_ANY, raw)
            except (json.JSONDecodeError, OSError):
                pass  # Settings file missing or corrupt; start fresh

        existing["hooks"] = hooks_config
        try:
            settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        except OSError:
            _logger.debug("Failed to write hooks config to %s", settings_path)

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
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        role = session_id.rsplit("-", 1)[0]  # e.g. "qa" from "qa-abc12345"

        # Inject Claude Code hooks for real-time tool-use and lifecycle monitoring.
        # Must happen before process launch so the agent picks up the config.
        self._inject_hooks_config(workdir, session_id)

        # Inject Bernstein MCP bridge so agents can report progress, check
        # sibling status, and read the bulletin board.  Uses the existing
        # bernstein.mcp.server module in stdio transport mode.
        bridge_server: dict[str, Any] = {
            "command": sys.executable,
            "args": ["-m", "bernstein.mcp.server"],
        }
        effective_mcp: dict[str, Any] = {}
        if mcp_config:
            if "mcpServers" in mcp_config:
                effective_mcp = {**mcp_config}
                effective_mcp["mcpServers"] = {**mcp_config["mcpServers"], "bernstein": bridge_server}
            else:
                effective_mcp = {"mcpServers": {**mcp_config, "bernstein": bridge_server}}
        else:
            effective_mcp = {"mcpServers": {"bernstein": bridge_server}}

        # Auto-detect batch mode: prompts starting with "/batch" are delegated
        # to Claude Code's built-in /batch skill, which requires more turns to
        # cover the full research → decompose → spawn-workers → track lifecycle.
        batch_mode = prompt.lstrip().startswith("/batch")
        if batch_mode:
            _logger.info("Batch mode detected for session %s — using %d max-turns", session_id, self.BATCH_MAX_TURNS)

        agents_json = build_agents_json(role)
        cmd = self._build_command(
            model_config,
            effective_mcp,
            prompt,
            role=role,
            workdir=workdir,
            agents_json=agents_json,
            system_addendum=system_addendum,
            batch_mode=batch_mode,
            task_scope=task_scope,
            budget_multiplier=budget_multiplier,
        )

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=role,
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        tokens_path = workdir / ".sdd" / "runtime" / f"{session_id}.tokens"
        heartbeat_dir = workdir / ".sdd" / "runtime" / "heartbeats"
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_path = heartbeat_dir / f"{session_id}.json"
        completed_dir = workdir / ".sdd" / "runtime" / "completed"
        completed_dir.mkdir(parents=True, exist_ok=True)
        completion_path = completed_dir / session_id
        wrapper = self._wrapper_script(
            session_id=session_id,
            tokens_path=str(tokens_path),
            heartbeat_path=str(heartbeat_path),
            completion_path=str(completion_path),
        )
        env = build_filtered_env(["ANTHROPIC_API_KEY"])
        claude_proc, wrapper_proc = self._launch_process(wrapped_cmd, wrapper, workdir, log_path, env=env)

        # Track the worker process (wraps claude) for is_alive/kill
        self._procs[claude_proc.pid] = claude_proc
        # Also track wrapper so we can kill both
        self._wrapper_pids[claude_proc.pid] = wrapper_proc.pid

        try:
            self._probe_fast_exit(claude_proc, log_path, provider_name="claude")
        except Exception:
            self._wrapper_pids.pop(claude_proc.pid, None)
            self._procs.pop(claude_proc.pid, None)
            with contextlib.suppress(Exception):
                wrapper_proc.wait(timeout=1)
            raise

        result = SpawnResult(pid=claude_proc.pid, log_path=log_path, proc=claude_proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(claude_proc.pid, timeout_seconds, session_id)
        return result

    def is_alive(self, pid: int) -> bool:
        # Use poll() to detect zombies — process_alive can't
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        # Fallback for processes we didn't spawn
        return process_alive(pid)

    def kill(self, pid: int) -> None:
        # The claude process is spawned with start_new_session=True, so
        # its PID equals its PGID.  Use the PID directly as PGID instead
        # of os.getpgid() which fails when the process is already dead —
        # this ensures we kill the entire session group including any
        # child processes (the actual claude CLI) that outlive the wrapper.
        #
        # ``kill_process_group_graceful`` sends SIGTERM, polls briefly, and
        # escalates to SIGKILL if the group is still alive.  Without the
        # escalation, agents that trap SIGTERM survive reap paths — see
        # audit-011.
        kill_process_group_graceful(pid)
        # Also kill the wrapper process with the same TERM→KILL escalation
        wrapper_pid = self._wrapper_pids.pop(pid, None)
        if wrapper_pid:
            kill_process_group_graceful(wrapper_pid)
        self._procs.pop(pid, None)

    def name(self) -> str:
        return "Claude Code"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Claude API tier based on environment and API key type.

        Checks ANTHROPIC_API_KEY prefix to determine tier:
        - sk-ant-api03... = Pro tier
        - sk-ant-api01... = Plus tier
        - Other = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        if not api_key:
            return None

        # Determine tier from API key prefix
        if api_key.startswith("sk-ant-api03"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=50000,
            )
        elif api_key.startswith("sk-ant-api01"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            )

        return ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
