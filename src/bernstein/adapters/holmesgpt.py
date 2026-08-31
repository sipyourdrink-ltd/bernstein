"""HolmesGPT adapter for Bernstein.

Fronts the HolmesGPT CLI for read-only investigation tasks. This adapter
content-addresses every consulted data source, binds them to conclusions,
and produces inconclusive receipts when appropriate.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from enum import StrEnum
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

# ---------------------------------------------------------------------------
# Typed terminal states
# ---------------------------------------------------------------------------


class HolmesGPTTerminalState(StrEnum):
    """Terminal outcome of a HolmesGPT run. Never free text.

    The orchestrator dispatches off these enum members so a driver crash or a
    wall-clock timeout is a structured state a compliance operator can act on,
    not a stack trace or a log grep.
    """

    OK = "ok"
    DRIVER_FAILURE = "driver_failure"
    TIMEOUT = "timeout"
    INCONCLUSIVE = "inconclusive"


class HolmesGPTDriverError(RuntimeError):
    """Raised when the concrete HolmesGPT driver faults.

    Carries the typed :class:`HolmesGPTTerminalState` so the failure never has
    to be re-parsed from a message string.
    """

    def __init__(self, message: str, *, terminal_state: HolmesGPTTerminalState) -> None:
        self.terminal_state = terminal_state
        super().__init__(message)


def classify_terminal_state(
    *, exit_code: int | None, timed_out: bool, inconclusive_detected: bool = False
) -> HolmesGPTTerminalState:
    """Map a raw driver exit into a typed terminal state.

    Args:
        exit_code: The driver process exit code, or ``None`` when it never
            produced one.
        timed_out: Whether the run hit its wall-clock timeout.
        inconclusive_detected: Whether the run produced inconclusive output.

    Returns:
        The corresponding :class:`HolmesGPTTerminalState`.
    """
    if timed_out:
        return HolmesGPTTerminalState.TIMEOUT
    if inconclusive_detected:
        return HolmesGPTTerminalState.INCONCLUSIVE
    if exit_code == 0:
        return HolmesGPTTerminalState.OK
    return HolmesGPTTerminalState.DRIVER_FAILURE


# ---------------------------------------------------------------------------
# Structured output types
# ---------------------------------------------------------------------------


@dataclass
class HolmesGPTStructuredOutput:
    """Parsed JSON output from HolmesGPT's --json-output flag."""

    conclusion: str
    """The final conclusion or answer from the investigation."""
    observations: list[str]
    """Content-addressed data sources consulted during the run."""
    sources: list[dict[str, str]]
    """Detailed source information with content hashes."""
    inconclusive: bool = False
    """Whether the investigation reached a definitive conclusion."""
    reasoning: str | None = None
    """Step-by-step reasoning trace, if available."""


def parse_structured_output(json_text: str) -> HolmesGPTStructuredOutput:
    """Parse HolmesGPT's structured JSON output into a typed dataclass.

    Args:
        json_text: Raw JSON string from HolmesGPT's --json-output.

    Returns:
        Populated :class:`HolmesGPTStructuredOutput`.

    Raises:
        json.JSONDecodeError: If the output is not valid JSON.
        KeyError: If required fields are missing from the JSON.
    """
    data = json.loads(json_text)
    return HolmesGPTStructuredOutput(
        conclusion=data["conclusion"],
        observations=data.get("observations", []),
        sources=data.get("sources", []),
        inconclusive=data.get("inconclusive", False),
        reasoning=data.get("reasoning"),
    )


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------


class HolmesGPTAdapter(CLIAdapter):
    """Adapter for the HolmesGPT CLI for read-only investigation.

    Content-addresses every consulted data source, binds them to conclusions,
    and produces inconclusive receipts when appropriate. Declares
    ARTIFACT output mode since investigation results are recorded as
    signed lineage artifacts, not git commits.
    """

    registry_name = "holmesgpt"
    is_multimodal = False

    def __init__(
        self,
        *,
        cli_command: str = "holmes",
        display_name: str = "HolmesGPT",
    ) -> None:
        super().__init__()
        self._cli_command = cli_command
        self._display_name = display_name

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
        # HolmesGPT is a read-only investigation tool that does not support
        # multimodal input - refuse if multimodal context is provided
        self.refuse_multimodal_if_needed(multimodal_context)

        # Create isolated working directory for this session
        isolated_workdir = workdir / ".sdd" / "holmesgpt" / session_id
        isolated_workdir.mkdir(parents=True, exist_ok=True)

        # Set up logging
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Prepare JSON output file path
        json_output_path = isolated_workdir / "output.json"

        # Build the HolmesGPT command
        cmd = [
            self._cli_command,
            "ask",  # or "investigate" - both should work for read-only tasks
            "--no-interactive",
            "--json-output",
            str(json_output_path),
            "--",
            prompt,
        ]

        # Wrap with bernstein-worker for process visibility
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

        # Prepare environment
        env = build_filtered_env()
        preexec_fn = self._get_preexec_fn()

        # Launch the process
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
                raise HolmesGPTDriverError(
                    f"{self._cli_command!r} not found in PATH",
                    terminal_state=HolmesGPTTerminalState.DRIVER_FAILURE,
                ) from exc
            except PermissionError as exc:
                raise HolmesGPTDriverError(
                    f"Permission denied executing {self._cli_command!r}: {exc}",
                    terminal_state=HolmesGPTTerminalState.DRIVER_FAILURE,
                ) from exc

        # Create spawn result and set up timeout watchdog
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return self._display_name
