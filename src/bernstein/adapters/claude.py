"""Claude Code CLI adapter."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, ClassVar, cast

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.claude_agents import build_agents_json
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

# Map short model names to Claude Code CLI model IDs
_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


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
                cfg = cast("dict[str, Any]", global_cfg)
                servers = cfg.get("mcpServers", cfg)
                if isinstance(servers, dict):
                    merged.update(cast("dict[str, Any]", servers))
        except (OSError, json.JSONDecodeError):
            pass

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
        d = cast("dict[str, Any]", obj)
        return {k: _resolve_env_vars(v) for k, v in d.items()}
    if isinstance(obj, list):
        lst = cast("list[Any]", obj)
        return [_resolve_env_vars(item) for item in lst]
    return obj


_logger = logging.getLogger(__name__)

# How long a cached rate-limit probe result stays valid (seconds).
_RATE_LIMIT_CACHE_TTL: float = 180.0  # 3 min — probe costs a real API call

# Cooldown applied when rate-limiting is detected (seconds).
_RATE_LIMIT_COOLDOWN: float = 300.0


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
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
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
    BATCH_MAX_TURNS: int = 200

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
        """
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        effort = getattr(model_config, "effort", "high")
        max_turns = (
            self.BATCH_MAX_TURNS
            if batch_mode
            else {"max": 100, "high": 50, "medium": 30, "normal": 25, "low": 15}.get(effort, 50)
        )
        effort_map = {"max": "max", "high": "high", "medium": "medium", "normal": "medium", "low": "low"}
        claude_effort = effort_map.get(effort, "high")

        # Choose fallback model: opus → sonnet, sonnet → haiku
        _fallback_map = {
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
            "--bare",  # Skip hooks, LSP, plugins, prefetches — 100-200ms faster startup
            "--max-turns",
            str(max_turns),
            "--output-format",
            "stream-json",
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

        # Per-task budget cap — prevents runaway token spend
        cmd.extend(["--max-budget-usd", "5.00"])

        if mcp_config:
            cmd.extend(["--mcp-config", json.dumps(mcp_config)])

        # Use --append-system-prompt-file for long addenda to avoid shell
        # argument length limits and quoting issues.
        if system_addendum:
            import tempfile

            fd, addendum_path = tempfile.mkstemp(suffix=".md", prefix="bernstein-prompt-")
            os.write(fd, system_addendum.encode())
            os.close(fd)
            cmd.extend(["--append-system-prompt-file", addendum_path])

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
                f"                open({tokens_path!r}, 'a').write(_rec + '\\n')\n"
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
                f"        open({heartbeat_path!r}, 'w').write(__import__('json').dumps(_hb))\n"
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
                f"            open({completion_path!r}, 'w').write(txt or 'done')\n"
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

        Uses try/finally to guarantee log_file is closed even if the wrapper
        Popen fails after claude has already started.

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
        try:
            try:
                claude_proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
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
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=workdir,
                    env=env,
                )
            except Exception:
                claude_proc.kill()
                raise
        finally:
            log_file.close()

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
        curl_cmd = f"curl -sS -X POST -H 'Content-Type: application/json' -d @- {hook_url}"
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
                    existing = cast("dict[str, Any]", raw)
            except (json.JSONDecodeError, OSError):
                pass

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
        # Use poll() to detect zombies — os.kill(pid, 0) can't
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        # Fallback for processes we didn't spawn
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, pid: int) -> None:
        # The claude process is spawned with start_new_session=True, so
        # its PID equals its PGID.  Use the PID directly as PGID instead
        # of os.getpgid() which fails when the process is already dead —
        # this ensures we kill the entire session group including any
        # child processes (the actual claude CLI) that outlive the wrapper.
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
        # Also kill the wrapper process
        wrapper_pid = self._wrapper_pids.pop(pid, None)
        if wrapper_pid:
            with contextlib.suppress(OSError):
                os.killpg(wrapper_pid, signal.SIGTERM)
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
