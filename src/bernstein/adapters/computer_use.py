"""Browser / computer-use adapter family (#2606).

This admits the one agent kind the coding-adapter substrate could not front:
*third-party autonomous* browser and computer-use agents. A coding adapter's
output is a file diff; a browser / computer-use agent owns its own decision loop
and emits a stream of GUI actions. This adapter fronts such an external agent and
leaves the per-action anchoring to
:mod:`bernstein.core.agents.computer_use_attestation`, so the boundary never
imports a specific browser tool -- the concrete driver stays behind the adapter
contract.

Two things live here:

* :class:`ComputerUseAdapter` -- a :class:`~bernstein.adapters.base.CLIAdapter`
  subclass adding per-task profile isolation and the capability-refusal path,
  the same on-ramp the coding adapters use.
* :class:`ReferenceComputerUseAdapter` -- a concrete reference adapter (registry
  name ``computer_use``) that proves the mechanism end to end without requiring a
  live browser in tests.

Driver failure and timeout never surface as free text: they map onto
:class:`ComputerUseTerminalState` via :func:`classify_terminal_state`, and a
driver fault is raised as a typed :class:`ComputerUseDriverError`.
"""

from __future__ import annotations

import subprocess
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


# ---------------------------------------------------------------------------
# Typed terminal states
# ---------------------------------------------------------------------------


class ComputerUseTerminalState(StrEnum):
    """Terminal outcome of a computer-use run. Never free text.

    The orchestrator dispatches off these enum members so a driver crash or a
    wall-clock timeout is a structured state a compliance operator can act on,
    not a stack trace or a log grep.
    """

    OK = "ok"
    DRIVER_FAILURE = "driver_failure"
    TIMEOUT = "timeout"
    REFUSED = "refused"


class ComputerUseDriverError(RuntimeError):
    """Raised when the concrete browser driver faults.

    Carries the typed :class:`ComputerUseTerminalState` so the failure never has
    to be re-parsed from a message string.
    """

    def __init__(self, message: str, *, terminal_state: ComputerUseTerminalState) -> None:
        self.terminal_state = terminal_state
        super().__init__(message)


def classify_terminal_state(*, exit_code: int | None, timed_out: bool) -> ComputerUseTerminalState:
    """Map a raw driver exit into a typed terminal state.

    Args:
        exit_code: The driver process exit code, or ``None`` when it never
            produced one.
        timed_out: Whether the run hit its wall-clock timeout.

    Returns:
        The corresponding :class:`ComputerUseTerminalState`.
    """
    if timed_out:
        return ComputerUseTerminalState.TIMEOUT
    if exit_code == 0:
        return ComputerUseTerminalState.OK
    return ComputerUseTerminalState.DRIVER_FAILURE


# ---------------------------------------------------------------------------
# Adapter family base
# ---------------------------------------------------------------------------


class ComputerUseAdapter(CLIAdapter):
    """Base for adapters that front a third-party browser / computer-use agent.

    Adds two things over :class:`CLIAdapter`:

    * **Per-task isolation.** :meth:`isolated_profile_dir` returns a per-session
      browser profile + working directory under the worktree, so two concurrent
      browser tasks share no cookie / profile state. The directory is a pure
      function of ``(workdir, session_id)``: distinct sessions get disjoint
      directories by construction.
    * **Capability gating.** Concrete adapters advertise
      :attr:`is_computer_use` so an incapable adapter fronting a browser task is
      refused before any process launch (parity with the multimodal boundary).
    """

    #: Concrete computer-use adapters set this to ``True``.
    is_computer_use: bool = False

    #: Relative root (under the worktree) for per-session profile isolation.
    _PROFILE_ROOT = (".sdd", "computer-use", "profiles")

    def isolated_profile_dir(self, *, workdir: Path, session_id: str) -> Path:
        """Return the per-session isolated profile directory.

        Two different ``session_id`` values always resolve to disjoint
        directories, so concurrent browser tasks cannot bleed cookies or
        profile state into each other.

        Args:
            workdir: The worktree working directory.
            session_id: The unique per-task session id.

        Returns:
            The profile directory path (not created).
        """
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)
        return workdir.joinpath(*self._PROFILE_ROOT, safe)

    def prepare_isolation(self, *, workdir: Path, session_id: str) -> Path:
        """Create and return the isolated profile directory for a session."""
        profile = self.isolated_profile_dir(workdir=workdir, session_id=session_id)
        profile.mkdir(parents=True, exist_ok=True)
        return profile


# ---------------------------------------------------------------------------
# Reference concrete adapter
# ---------------------------------------------------------------------------


class ReferenceComputerUseAdapter(ComputerUseAdapter):
    """Reference computer-use adapter fronting an external browser agent.

    The concrete browser driver stays behind the adapter contract: this adapter
    only launches the external agent process (isolated to a per-session profile)
    and lets the boundary layer anchor each action. It requires no live browser
    in tests -- the per-action anchoring mechanism is exercised directly through
    :mod:`bernstein.core.agents.computer_use_attestation`.

    Args:
        cli_command: The external agent CLI to launch. Defaults to a reference
            placeholder; a real deployment points this at the partner browser
            agent binary.
        display_name: Human-readable adapter name.
    """

    registry_name = "computer_use"
    is_computer_use = True

    def __init__(
        self,
        *,
        cli_command: str = "computer-use-agent",
        display_name: str = "Computer Use",
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
        # Capability gating parity: a browser task handed to an incapable
        # adapter is refused up front. This adapter IS capable, but an operator
        # attaching an image to it still hits the multimodal boundary refusal
        # (this adapter fronts actions, not attachments).
        self.refuse_multimodal_if_needed(multimodal_context)

        # Per-task isolation: distinct sessions get disjoint profile dirs.
        profile_dir = self.prepare_isolation(workdir=workdir, session_id=session_id)

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._cli_command,
            "--model",
            model_config.model,
            "--user-data-dir",
            str(profile_dir),
            "--prompt",
            prompt,
        ]

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

        env = build_filtered_env()
        preexec_fn = self._get_preexec_fn()
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
                raise ComputerUseDriverError(
                    f"{self._cli_command!r} not found in PATH",
                    terminal_state=ComputerUseTerminalState.DRIVER_FAILURE,
                ) from exc
            except PermissionError as exc:
                raise ComputerUseDriverError(
                    f"Permission denied executing {self._cli_command!r}: {exc}",
                    terminal_state=ComputerUseTerminalState.DRIVER_FAILURE,
                ) from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return self._display_name


__all__ = [
    "ComputerUseAdapter",
    "ComputerUseDriverError",
    "ComputerUseTerminalState",
    "ReferenceComputerUseAdapter",
    "classify_terminal_state",
]
