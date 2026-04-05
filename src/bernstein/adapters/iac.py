"""Infrastructure-as-Code (Terraform/Pulumi) adapter for Bernstein.

Orchestrates IaC agents that run plan/preview before apply, enforcing
a dry-run safety check on every spawn.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnResult,
    build_worker_cmd,
)
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# IaC tool definitions: (binary, plan command args, apply command args)
_TOOL_DEFS: dict[str, tuple[list[str], list[str]]] = {
    "terraform": (
        ["terraform", "plan", "-no-color", "-detailed-exitcode"],
        ["terraform", "apply", "-auto-approve", "-no-color"],
    ),
    "pulumi": (
        ["pulumi", "preview", "--non-interactive"],
        ["pulumi", "up", "--yes", "--non-interactive"],
    ),
}


def _detect_tool() -> str | None:
    """Return the first available IaC tool name, or None."""
    for tool_name in _TOOL_DEFS:
        if shutil.which(tool_name) is not None:
            return tool_name
    return None


def _build_iac_script(tool: str, prompt: str) -> str:
    """Build a shell script that runs plan/preview, then apply only on success."""
    plan_cmd, apply_cmd = _TOOL_DEFS[tool]
    plan_str = " ".join(plan_cmd)
    apply_str = " ".join(apply_cmd)

    if tool == "terraform":
        # Exit code 2 = changes to apply, 0 = no changes, 1 = error
        check_block = (
            "plan_exit=$?\n"
            "if [ $plan_exit -eq 1 ]; then\n"
            '  echo "ERROR: terraform plan failed" >&2\n'
            "  exit 1\n"
            "fi\n"
            "if [ $plan_exit -eq 0 ]; then\n"
            '  echo "No changes detected."\n'
            "  exit 0\n"
            "fi\n"
        )
    else:
        check_block = 'if [ $? -ne 0 ]; then\n  echo "ERROR: pulumi preview failed" >&2\n  exit 1\nfi\n'

    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'echo "Task: {prompt}"\n'
        f'echo "--- Running {tool} plan ---"\n'
        f"{plan_str}\n"
        f"{check_block}"
        f'echo "--- Applying changes ---"\n'
        f"{apply_str}\n"
    )


class IaCAdapter(CLIAdapter):
    """Adapter for Infrastructure-as-Code tools (Terraform / Pulumi).

    Detects which IaC CLI is available and spawns a worker that always
    runs a dry-run (plan/preview) before apply.
    """

    def __init__(self, *, tool: str | None = None) -> None:
        """Force a specific tool, or None to auto-detect at spawn time."""
        if tool is not None and tool not in _TOOL_DEFS:
            msg = f"Unknown IaC tool {tool!r}. Supported: {', '.join(_TOOL_DEFS)}"
            raise ValueError(msg)
        self._tool = tool

    def _resolve_tool(self) -> str:
        """Return the IaC tool to use, detecting if needed."""
        if self._tool is not None:
            return self._tool
        detected = _detect_tool()
        if detected is None:
            raise RuntimeError("No IaC tool found. Install terraform or pulumi.")
        return detected

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> SpawnResult:
        """Spawn an IaC plan-then-apply process."""
        tool = self._resolve_tool()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the plan-then-apply script
        script_path = workdir / ".sdd" / "runtime" / f"{session_id}-iac.sh"
        script_content = _build_iac_script(tool, prompt)
        script_path.write_text(script_content, encoding="utf-8")
        script_path.chmod(0o755)

        cmd = ["bash", str(script_path)]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=f"iac:{tool}",
        )

        env = build_filtered_env(
            [
                "TF_VAR_region",
                "TF_TOKEN_app_terraform_io",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_DEFAULT_REGION",
                "ARM_CLIENT_ID",
                "ARM_CLIENT_SECRET",
                "ARM_SUBSCRIPTION_ID",
                "ARM_TENANT_ID",
                "GOOGLE_CREDENTIALS",
                "GOOGLE_PROJECT",
                "PULUMI_ACCESS_TOKEN",
            ]
        )

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
                raise RuntimeError(f"{tool} not found in PATH") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {tool}: {exc}") from exc

        timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc, timeout_timer=timer)

    def name(self) -> str:
        """Human-readable adapter name."""
        return "IaC (Terraform/Pulumi)"

    def is_available(self) -> bool:
        """Return True if at least one IaC tool is on PATH."""
        return _detect_tool() is not None
