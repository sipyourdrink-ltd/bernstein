"""Comprehensive health-check command for Bernstein.

CLI-006: ``bernstein doctor`` comprehensive health checks.

Checks: adapters installed, API keys set, config valid, disk space,
git installed, server reachable, and more.  Delegates to the existing
``status_cmd.doctor`` implementation and adds new checks for disk
space and git availability.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import SERVER_URL

_TASK_SERVER_LABEL = "Task server"

_CONFIG_FILE_LABEL = "Config file"

# ---------------------------------------------------------------------------
# Health check dataclass
# ---------------------------------------------------------------------------

_CHECK_PASS = "PASS"
_CHECK_FAIL = "FAIL"
_CHECK_WARN = "WARN"


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python_version() -> dict[str, Any]:
    """Check Python version >= 3.12."""
    major, minor = sys.version_info.major, sys.version_info.minor
    ok = (major, minor) >= (3, 12)
    return {
        "name": "Python version",
        "status": _CHECK_PASS if ok else _CHECK_FAIL,
        "detail": f"{major}.{minor}",
        "fix": "Install Python 3.12 or newer" if not ok else "",
    }


def check_adapters_installed() -> list[dict[str, Any]]:
    """Check which CLI adapters are on PATH."""
    results: list[dict[str, Any]] = []
    for name in ("claude", "codex", "gemini", "qwen", "aider"):
        found = shutil.which(name) is not None
        results.append(
            {
                "name": f"Adapter: {name}",
                "status": _CHECK_PASS if found else _CHECK_WARN,
                "detail": "found in PATH" if found else "not in PATH",
                "fix": f"Install {name} CLI" if not found else "",
            }
        )
    return results


def check_api_keys() -> list[dict[str, Any]]:
    """Check environment variables for common API keys."""
    results: list[dict[str, Any]] = []
    keys = {
        "ANTHROPIC_API_KEY": "Claude",
        "OPENAI_API_KEY": "Codex / OpenAI",
        "GOOGLE_API_KEY": "Gemini",
    }
    for env_var, label in keys.items():
        present = bool(os.environ.get(env_var))
        results.append(
            {
                "name": f"API key: {label}",
                "status": _CHECK_PASS if present else _CHECK_WARN,
                "detail": f"{env_var} set" if present else f"{env_var} not set",
                "fix": f"export {env_var}=<your-key>" if not present else "",
            }
        )
    return results


def check_config_valid() -> dict[str, Any]:
    """Check that bernstein.yaml (if present) is valid YAML."""
    yaml_path = Path.cwd() / "bernstein.yaml"
    if not yaml_path.exists():
        return {
            "name": _CONFIG_FILE_LABEL,
            "status": _CHECK_WARN,
            "detail": "bernstein.yaml not found",
            "fix": "Run 'bernstein init' to create one",
        }
    try:
        import yaml

        with open(yaml_path) as f:
            yaml.safe_load(f)
        return {
            "name": _CONFIG_FILE_LABEL,
            "status": _CHECK_PASS,
            "detail": f"bernstein.yaml valid ({yaml_path})",
            "fix": "",
        }
    except Exception as exc:
        return {
            "name": _CONFIG_FILE_LABEL,
            "status": _CHECK_FAIL,
            "detail": f"bernstein.yaml parse error: {exc}",
            "fix": "Fix YAML syntax in bernstein.yaml",
        }


def check_disk_space() -> dict[str, Any]:
    """Check available disk space (warn if < 1 GB)."""
    try:
        usage = shutil.disk_usage(Path.cwd())
        free_gb = usage.free / (1024**3)
        ok = free_gb >= 1.0
        return {
            "name": "Disk space",
            "status": _CHECK_PASS if ok else _CHECK_WARN,
            "detail": f"{free_gb:.1f} GB free ({_format_bytes(usage.free)})",
            "fix": "Free up disk space" if not ok else "",
        }
    except Exception as exc:
        return {
            "name": "Disk space",
            "status": _CHECK_WARN,
            "detail": f"could not check: {exc}",
            "fix": "",
        }


def check_git_installed() -> dict[str, Any]:
    """Check that git is installed and accessible."""
    git_path = shutil.which("git")
    if not git_path:
        return {
            "name": "Git",
            "status": _CHECK_FAIL,
            "detail": "git not found in PATH",
            "fix": "Install git: https://git-scm.com/",
        }
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        version = result.stdout.strip()
        return {
            "name": "Git",
            "status": _CHECK_PASS,
            "detail": version,
            "fix": "",
        }
    except Exception as exc:
        return {
            "name": "Git",
            "status": _CHECK_WARN,
            "detail": f"git found but error: {exc}",
            "fix": "",
        }


def check_server_reachable() -> dict[str, Any]:
    """Check if the Bernstein task server is reachable."""
    try:
        import httpx

        resp = httpx.get(f"{SERVER_URL}/health", timeout=2.0)
        if resp.status_code == 200:
            return {
                "name": _TASK_SERVER_LABEL,
                "status": _CHECK_PASS,
                "detail": f"reachable at {SERVER_URL}",
                "fix": "",
            }
        return {
            "name": _TASK_SERVER_LABEL,
            "status": _CHECK_WARN,
            "detail": f"returned {resp.status_code}",
            "fix": "Start with 'bernstein run'",
        }
    except Exception:
        return {
            "name": _TASK_SERVER_LABEL,
            "status": _CHECK_WARN,
            "detail": "not running",
            "fix": "Start with 'bernstein run'",
        }


def check_port_available() -> dict[str, Any]:
    """Check if port 8052 is available or already in use by Bernstein."""
    port = 8052
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            in_use = result == 0
    except Exception:
        in_use = False

    if in_use:
        return {
            "name": f"Port {port}",
            "status": _CHECK_WARN,
            "detail": "in use (server may already be running)",
            "fix": "Run 'bernstein stop' to free the port",
        }
    return {
        "name": f"Port {port}",
        "status": _CHECK_PASS,
        "detail": "available",
        "fix": "",
    }


def check_sdd_workspace() -> dict[str, Any]:
    """Check for .sdd/ workspace structure."""
    workdir = Path.cwd()
    required = [".sdd", ".sdd/backlog", ".sdd/runtime"]
    missing = [d for d in required if not (workdir / d).exists()]
    if missing:
        return {
            "name": ".sdd workspace",
            "status": _CHECK_WARN,
            "detail": f"missing: {', '.join(missing)}",
            "fix": "Run 'bernstein init' to create workspace",
        }
    return {
        "name": ".sdd workspace",
        "status": _CHECK_PASS,
        "detail": "present",
        "fix": "",
    }


def run_all_checks() -> list[dict[str, Any]]:
    """Run all health checks and return results."""
    checks: list[dict[str, Any]] = []
    checks.append(check_python_version())
    checks.extend(check_adapters_installed())
    checks.extend(check_api_keys())
    checks.append(check_config_valid())
    checks.append(check_disk_space())
    checks.append(check_git_installed())
    checks.append(check_server_reachable())
    checks.append(check_port_available())
    checks.append(check_sdd_workspace())
    return checks


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--fix", "auto_fix", is_flag=True, default=False, help="Attempt to auto-fix issues.")
@click.pass_context
def doctor_cmd(ctx: click.Context, as_json: bool, auto_fix: bool) -> None:
    """Run comprehensive health checks on the Bernstein installation.

    \b
    Checks:
      - Python version (>= 3.12)
      - CLI adapters installed (claude, codex, gemini, qwen, aider)
      - API keys set (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY)
      - Config file valid (bernstein.yaml)
      - Disk space (>= 1 GB free)
      - Git installed and accessible
      - Task server reachable
      - Port 8052 available
      - .sdd workspace structure

    \b
    Examples:
      bernstein doctor            # print diagnostic report
      bernstein doctor --json     # machine-readable output
      bernstein doctor --fix      # attempt to auto-fix issues
    """
    # Delegate to the existing full doctor implementation which has more checks
    from bernstein.cli.status_cmd import doctor as _doctor_impl

    ctx.invoke(_doctor_impl, as_json=as_json, auto_fix=auto_fix)
