"""Claude Code CLI adapter."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar, cast

from bernstein.adapters.base import CLIAdapter, SpawnResult, build_worker_cmd
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


class ClaudeCodeAdapter(CLIAdapter):
    """Spawn and monitor Claude Code CLI sessions."""

    # Track Popen objects for reliable is_alive() via poll()
    _procs: ClassVar[dict[int, subprocess.Popen[bytes]]] = {}
    _wrapper_pids: ClassVar[dict[int, int]] = {}  # claude_pid → wrapper_pid

    def _build_command(
        self,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        prompt: str,
    ) -> list[str]:
        """Build the claude CLI command with effort mapping."""
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        effort = getattr(model_config, "effort", "high")
        max_turns = {"max": 100, "high": 50, "medium": 30, "normal": 25, "low": 15}.get(effort, 50)
        effort_map = {"max": "max", "high": "high", "medium": "medium", "normal": "medium", "low": "low"}
        claude_effort = effort_map.get(effort, "high")

        cmd = [
            "claude",
            "--model",
            model_id,
            "--effort",
            claude_effort,
            "--dangerously-skip-permissions",
            "--max-turns",
            str(max_turns),
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if mcp_config:
            cmd.extend(["--mcp-config", json.dumps(mcp_config)])
        cmd.extend(["-p", prompt])
        return cmd

    @staticmethod
    def _wrapper_script(session_id: str = "", tokens_path: str = "") -> str:
        """Return the stream-json → human-readable log converter script.

        When ``session_id`` and ``tokens_path`` are provided, token usage from
        ``result`` messages is appended to ``tokens_path`` as JSON-lines so the
        token growth monitor can read it without re-parsing the full log.

        Args:
            session_id: Agent session ID, injected for token sidecar writes.
            tokens_path: Absolute path to the ``.tokens`` sidecar file.
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
            "        continue\n"
            "    t = msg.get('type', '')\n"
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
            "            print(txt, flush=True)\n" + token_writer
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

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command(model_config, mcp_config, prompt)

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],  # e.g. "qa" from "qa-abc12345"
            session_id=session_id,
            pid_dir=pid_dir,
            model=model_id,
        )

        tokens_path = workdir / ".sdd" / "runtime" / f"{session_id}.tokens"
        wrapper = self._wrapper_script(session_id=session_id, tokens_path=str(tokens_path))
        env = build_filtered_env(["ANTHROPIC_API_KEY"])
        claude_proc, wrapper_proc = self._launch_process(wrapped_cmd, wrapper, workdir, log_path, env=env)

        # Track the worker process (wraps claude) for is_alive/kill
        self._procs[claude_proc.pid] = claude_proc
        # Also track wrapper so we can kill both
        self._wrapper_pids[claude_proc.pid] = wrapper_proc.pid

        return SpawnResult(pid=claude_proc.pid, log_path=log_path, proc=claude_proc)

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
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        # Also kill the wrapper process
        wrapper_pid = self._wrapper_pids.pop(pid, None)
        if wrapper_pid:
            with contextlib.suppress(OSError):
                os.kill(wrapper_pid, signal.SIGTERM)
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
