"""Pre-flight checks: validate CLI, API key, port availability before bootstrap.

These checks run before any system state is modified, ensuring we fail fast
with actionable error messages if prerequisites are missing.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from rich.console import Console

logger = logging.getLogger(__name__)

console = Console()


# Use ASCII-safe symbols on Windows with legacy encoding
def _safe_symbol(unicode_char: str, ascii_fallback: str) -> str:
    """Return unicode_char if encodable, else ascii_fallback."""
    if sys.platform == "win32":
        try:
            unicode_char.encode(sys.stdout.encoding or "utf-8")
        except (UnicodeEncodeError, LookupError):
            return ascii_fallback
    return unicode_char


_CHECK = _safe_symbol("✓", "+")
_ARROW = _safe_symbol("↳", "->")

# Binary install hints for each supported CLI adapter.
_CLI_INSTALL_HINT: dict[str, str] = {
    "claude": "https://claude.ai/code",
    "codex": "npm install -g @openai/codex",
    "gemini": "npm install -g @google/gemini-cli",
    "qwen": "npm install -g qwen-code",
}

# Primary API key env var(s) per adapter.
_CLI_API_KEY_ENV: dict[str, str] = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}

# Qwen supports multiple providers; any one of these is sufficient.
_QWEN_API_KEY_VARS: tuple[str, ...] = (
    "OPENROUTER_API_KEY_PAID",
    "OPENROUTER_API_KEY_FREE",
    "OPENAI_API_KEY",
    "TOGETHERAI_USER_KEY",
    "OXen_API_KEY",
    "G4F_API_KEY",
)


def _check_binary(cli: str) -> None:
    """Exit with an actionable message if the CLI binary is not in PATH.

    Args:
        cli: Adapter name (e.g. "claude", "codex", "gemini", "qwen").

    Raises:
        SystemExit: If the binary is not found.
    """
    from bernstein.cli.errors import BernsteinError

    binary = cli  # binary name matches adapter name for all supported adapters
    if shutil.which(binary) is None:
        hint = _CLI_INSTALL_HINT.get(cli, f"See documentation for {binary!r}")
        BernsteinError(
            what=f"{binary!r} not found in PATH",
            why=f"The {cli} CLI adapter is required but not installed",
            fix=f"Install: {hint}",
        ).print()
        raise SystemExit(1)


def _claude_has_oauth_session() -> bool:
    """Check if Claude Code has an active OAuth session (no API key needed)."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # If claude --version works, the binary is functional.
        # Claude Code with OAuth doesn't need ANTHROPIC_API_KEY.
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _gemini_has_gcloud_auth() -> bool:
    """Check if Gemini CLI can authenticate via gcloud Application Default Credentials.

    Runs ``gcloud auth list`` and looks for an active account.

    Returns:
        True if an active gcloud account is detected.
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "list", "--format=value(account,status)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        # Output lines like "user@example.com ACTIVE"
        for line in result.stdout.strip().splitlines():
            if "ACTIVE" in line.upper():
                return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


def gemini_has_auth() -> tuple[bool, str]:
    """Check all supported Gemini authentication methods.

    Checks (in order):
    1. ``GEMINI_API_KEY`` env var
    2. ``GOOGLE_API_KEY`` env var
    3. ``GOOGLE_APPLICATION_CREDENTIALS`` env var
    4. ``gcloud auth`` (Application Default Credentials)
    5. ``~/.config/gemini/`` config directory

    Returns:
        Tuple of (authenticated, method_description).
    """
    if os.environ.get("GEMINI_API_KEY"):
        return True, "GEMINI_API_KEY"
    if os.environ.get("GOOGLE_API_KEY"):
        return True, "GOOGLE_API_KEY"
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        if Path(creds_path).exists():
            return True, "GOOGLE_APPLICATION_CREDENTIALS"
    if _gemini_has_gcloud_auth():
        return True, "gcloud auth"
    # Gemini CLI native auth: ~/.gemini/google_accounts.json
    _accounts_file = Path.home() / ".gemini" / "google_accounts.json"
    if _accounts_file.exists():
        try:
            import json as _json

            _data = _json.loads(_accounts_file.read_text())
            _active = _data.get("active", "")
            if _active:
                return True, f"Google OAuth ({_active})"
        except Exception:
            pass
    config_dir = Path.home() / ".config" / "gemini"
    if config_dir.exists() and any(config_dir.iterdir()):
        return True, "config"
    return False, ""


def _codex_has_login() -> bool:
    """Check if Codex CLI is logged in via ChatGPT or other CLI auth.

    Runs ``codex login status`` and looks for a positive login indicator.

    Returns:
        True if codex reports being logged in.
    """
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        combined = (result.stdout + result.stderr).lower()
        return "logged in" in combined and "not logged in" not in combined and result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _codex_has_config_toml() -> tuple[bool, str | None]:
    """Check if ~/.codex/config.toml exists with a model configured.

    Returns:
        Tuple of (has_config, model_name).
    """
    from pathlib import Path

    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return False, None

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            return False, None

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        model = config.get("model")
        if isinstance(model, str) and model:
            return True, model
    except Exception:
        pass
    return False, None


def _codex_has_auth() -> tuple[bool, str]:
    """Check all supported Codex authentication methods.

    Checks (in order):
    1. ``OPENAI_API_KEY`` env var
    2. ``codex login status`` (ChatGPT / CLI login)
    3. ``~/.codex/config.toml`` with model configured (proxy setup)

    Returns:
        Tuple of (authenticated, method_description).
    """
    if os.environ.get("OPENAI_API_KEY"):
        return True, "OPENAI_API_KEY"
    if _codex_has_login():
        return True, "ChatGPT login"
    has_config, model = _codex_has_config_toml()
    if has_config:
        return True, f"config.toml ({model})"
    return False, ""


def _check_api_key(cli: str) -> None:
    """Exit with an actionable message if the required API key is not set.

    For Claude Code: skips the check if an OAuth session is active (no API key
    needed when using `claude` CLI with built-in auth).

    Args:
        cli: Adapter name (e.g. "claude", "codex", "gemini", "qwen").

    Raises:
        SystemExit: If the required API key env var is missing.
    """
    from bernstein.cli.errors import BernsteinError

    error = _get_api_key_error(cli)
    if error is not None:
        BernsteinError(what=error[0], why=error[1], fix=error[2]).print()
        raise SystemExit(1)


def _get_api_key_error(cli: str) -> tuple[str, str, str] | None:
    """Return (what, why, fix) tuple if API key check fails, or None if OK."""
    if cli == "qwen":
        if not any(os.environ.get(v) for v in _QWEN_API_KEY_VARS):
            return (
                "No API key configured for qwen",
                "Qwen requires one of: " + ", ".join(_QWEN_API_KEY_VARS),
                "export OPENROUTER_API_KEY_PAID=your-key (or any supported key var)",
            )
    elif cli == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY") and not _claude_has_oauth_session():
            return (
                "No Claude authentication found",
                "Neither ANTHROPIC_API_KEY nor an active OAuth session was detected",
                "export ANTHROPIC_API_KEY=your-key, or log in via: claude login",
            )
    elif cli == "gemini":
        authenticated, _method = gemini_has_auth()
        if not authenticated:
            return (
                "No Gemini authentication found",
                "None of GEMINI_API_KEY, GOOGLE_API_KEY, "
                "GOOGLE_APPLICATION_CREDENTIALS, gcloud auth, or "
                "~/.config/gemini/ were detected",
                "export GOOGLE_API_KEY=your-key, or run: gcloud auth login",
            )
    elif cli == "codex":
        authenticated, _method = _codex_has_auth()
        if not authenticated:
            return (
                "No Codex authentication found",
                "Neither OPENAI_API_KEY nor an active ChatGPT login was detected",
                "export OPENAI_API_KEY=your-key, or run: codex login",
            )
    else:
        env_var = _CLI_API_KEY_ENV.get(cli)
        if env_var and not os.environ.get(env_var):
            return (
                f"{cli} adapter requires an API key",
                f"Environment variable {env_var} is not set",
                f"export {env_var}=your-api-key",
            )
    return None


def _check_port_free(port: int) -> None:
    """Ensure the port is free, killing a stale Bernstein server if needed.

    If a previous Bernstein server is still occupying the port (common after
    crashes or force-quit), this function kills it automatically instead of
    failing with an error.  Non-Bernstein processes are NOT killed.

    Args:
        port: TCP port to check.

    Raises:
        SystemExit: If the port is occupied by a non-Bernstein process.
    """
    import subprocess

    from bernstein.cli.errors import port_in_use

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return  # Port is free.
        except OSError:
            pass

    # Port is occupied — try to identify and kill a stale Bernstein server.
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split() if p.isdigit()]
    except Exception:
        pids = []

    if not pids:
        port_in_use(port).print()
        raise SystemExit(1)

    from bernstein.core.platform_compat import kill_process

    # Only kill processes that look like Bernstein (uvicorn server).
    killed = False
    for pid in pids:
        try:
            proc_result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            cmd = proc_result.stdout.strip()
            if "bernstein" in cmd or "uvicorn" in cmd:
                kill_process(pid, sig=9)
                killed = True
                logger.info("Killed stale Bernstein process %d on port %d", pid, port)
        except OSError:
            continue

    if killed:
        import time

        time.sleep(0.5)  # Brief wait for port to release.
        return

    port_in_use(port).print()
    raise SystemExit(1)


def preflight_checks(cli: str, port: int) -> None:
    """Run pre-flight checks before starting the server.

    Verifies that:
    1. The CLI binary is installed and in PATH.
    2. The required API key env var is present.
    3. The server port is not already occupied.

    Args:
        cli: Adapter name (e.g. "claude", "codex", "gemini", "qwen", "mock").
        port: TCP port the server will bind to.

    Raises:
        SystemExit: On any pre-flight failure, with an actionable message.
    """
    if cli == "mock":
        # Mock adapter: no binary or API key needed (built-in simulation)
        console.print(f"[green]{_CHECK}[/green] Mock adapter ready (no API key needed)")
    elif cli == "auto":
        # Auto mode: use agent_discovery for rich detection with auth + model info
        from bernstein.core.agent_discovery import discover_agents_cached, short_model

        discovery = discover_agents_cached()
        if not discovery.agents:
            console.print("[bold red]Error:[/bold red] No CLI agents found. Install at least one:")
            for name, hint in _CLI_INSTALL_HINT.items():
                console.print(f"  {name}: {hint}")
            raise SystemExit(1)

        # Build "Claude (sonnet/opus), Codex (o4-mini)" description
        agent_parts: list[str] = []
        for agent in discovery.agents:
            short_models = [short_model(m) for m in agent.available_models[:2]]
            auth_note = "" if agent.logged_in else " [dim](not authenticated)[/dim]"
            agent_parts.append(f"{agent.name.capitalize()} ({'/'.join(short_models)}){auth_note}")
        routing_note = "Using auto-routing." if len(discovery.agents) > 1 else "Using as primary."
        console.print(f"[green]{_CHECK}[/green] Found: {', '.join(agent_parts)}. {routing_note}")

        # Surface any auth warnings as hints
        for w in discovery.warnings:
            console.print(f"  [yellow]{_ARROW}[/yellow] {w}")
    else:
        _check_binary(cli)
        _check_api_key(cli)
    _check_port_free(port)
