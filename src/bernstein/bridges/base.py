"""Abstract RuntimeBridge interface for external runtime integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


def _str_any_dict() -> dict[str, Any]:
    return {}


def _str_str_dict() -> dict[str, str]:
    return {}


class AgentState(StrEnum):
    """Lifecycle state of an agent running in an external runtime."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RemoteRunState(StrEnum):
    """Observed service lifecycle, independent of local task acceptance."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Whether the observed lifecycle ended; never delivery or authority."""
        return self in (RemoteRunState.COMPLETED, RemoteRunState.FAILED, RemoteRunState.CANCELED)


class RemoteCancelOutcome(StrEnum):
    """What an adapter observed about a remote cancellation request."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    UNKNOWN = "unknown"


def _validate_remote_identity(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _validate_remote_timestamp(value: float, name: str) -> None:
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be epoch seconds as an int or float, not bool")
    try:
        valid = isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite nonnegative epoch seconds")


@dataclass(frozen=True)
class RemoteRunHandle:
    """Immutable service/run binding; validation does not authenticate it.

    Supplied strings and timestamps are preserved without normalization.
    ``console_url`` is an optional nonblank label, never followed here.
    """

    service: str
    remote_run_id: str
    adapter_version: str
    submitted_at: float
    console_url: str | None = None

    def __post_init__(self) -> None:
        _validate_remote_identity(self.service, "service")
        _validate_remote_identity(self.remote_run_id, "remote_run_id")
        _validate_remote_identity(self.adapter_version, "adapter_version")
        _validate_remote_timestamp(self.submitted_at, "submitted_at")
        if self.console_url is not None:
            _validate_remote_identity(self.console_url, "console_url")


def _validate_remote_observation(handle: RemoteRunHandle, observed_at: float, detail: str) -> None:
    if not isinstance(handle, RemoteRunHandle):
        raise TypeError("handle must be a RemoteRunHandle")
    _validate_remote_timestamp(observed_at, "observed_at")
    if not isinstance(detail, str):
        raise TypeError("detail must be a string")


@dataclass(frozen=True)
class RemoteRunStatus:
    """A typed service observation, not local acceptance or authorization."""

    handle: RemoteRunHandle
    state: RemoteRunState
    observed_at: float
    detail: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, RemoteRunState):
            raise TypeError("state must be a RemoteRunState")
        _validate_remote_observation(self.handle, self.observed_at, self.detail)


@dataclass(frozen=True)
class RemoteCancel:
    """Observed cancellation outcome, without granting fallback or closure."""

    handle: RemoteRunHandle
    outcome: RemoteCancelOutcome
    observed_at: float
    detail: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RemoteCancelOutcome):
            raise TypeError("outcome must be a RemoteCancelOutcome")
        _validate_remote_observation(self.handle, self.observed_at, self.detail)


@dataclass(frozen=True)
class BridgeConfig:
    """Configuration for a RuntimeBridge instance.

    Attributes:
        bridge_type: Identifier for the bridge implementation (e.g. "openclaw").
        endpoint: Base URL or socket address of the runtime API.
        api_key: Credential for authenticating with the runtime.
        timeout_seconds: HTTP request timeout for all bridge calls.
        max_log_bytes: Maximum bytes to return from a single logs() call.
        extra: Bridge-specific options not covered by the standard fields.
    """

    bridge_type: str
    endpoint: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: int = 30
    max_log_bytes: int = 1_048_576  # 1 MiB
    extra: dict[str, Any] = field(default_factory=_str_any_dict)


@dataclass
class SpawnRequest:
    """Parameters for spawning an agent in an external runtime.

    Attributes:
        agent_id: Caller-assigned unique identifier for this agent run.
        image: Container image or runtime environment tag.
        command: Entrypoint command to execute.
        prompt: Fully rendered task prompt for runtimes that execute an
            embedded agent loop instead of a raw process command.
        env: Environment variables injected into the agent process.
        workdir: Working directory path inside the runtime.
        cpu_limit: CPU cores (fractional allowed, e.g. 0.5).
        memory_mb: Memory limit in mebibytes.
        timeout_seconds: Hard wall-clock limit; runtime must enforce this.
        log_path: Preferred Bernstein-side log destination for captured output.
        role: Bernstein role assigned to this agent.
        model: Model name selected by Bernstein for this run.
        effort: Reasoning effort selected by Bernstein for this run.
        labels: Arbitrary key/value metadata attached to the run.
    """

    agent_id: str
    image: str
    command: list[str]
    prompt: str = ""
    env: dict[str, str] = field(default_factory=_str_str_dict)
    workdir: str = "/workspace"
    cpu_limit: float = 1.0
    memory_mb: int = 512
    timeout_seconds: int = 300
    log_path: str = ""
    role: str = ""
    model: str = ""
    effort: str = ""
    labels: dict[str, str] = field(default_factory=_str_str_dict)


@dataclass(frozen=True)
class AgentStatus:
    """Point-in-time status of an agent run.

    Attributes:
        agent_id: Identifier matching the one supplied in SpawnRequest.
        state: Current lifecycle state.
        exit_code: Process exit code, or None if still running.
        started_at: Unix timestamp when the run started, or None.
        finished_at: Unix timestamp when the run finished, or None.
        message: Optional human-readable status detail.
        metadata: Bridge-specific fields such as remote session/run identifiers.
    """

    agent_id: str
    state: AgentState
    exit_code: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    message: str = ""
    metadata: dict[str, str] = field(default_factory=_str_str_dict)


class RuntimeBridge(ABC):
    """Interface for launching and monitoring agents in an external runtime.

    Implement this for each supported execution backend (OpenClaw, Kubernetes,
    Docker, Firecracker microVMs, etc.).  The orchestrator calls these methods;
    it never imports a concrete subclass directly - all wiring goes through the
    bridge registry.

    All methods are async to allow non-blocking I/O against remote APIs.
    """

    def __init__(self, config: BridgeConfig) -> None:
        """Initialise the bridge with validated configuration.

        Args:
            config: Bridge configuration; validated by the caller before
                    construction.
        """
        self._config = config

    @property
    def config(self) -> BridgeConfig:
        """The configuration this bridge was initialised with."""
        return self._config

    async def remote_status(self, handle: RemoteRunHandle) -> RemoteRunStatus:
        """Optionally observe a remote run; unsupported bridges refuse.

        This never falls back to legacy ``status`` or infers remote identity
        from an agent id. No runtime caller is introduced by this contract.
        """
        raise BridgeCapabilityError("remote_status")

    async def cancel_remote(self, handle: RemoteRunHandle) -> RemoteCancel:
        """Optionally observe cancellation; legacy ``cancel`` is not called.

        A legacy ``None`` return cannot establish confirmed termination.
        """
        raise BridgeCapabilityError("cancel_remote")

    @abstractmethod
    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        """Launch an agent in the external runtime.

        The method must return as soon as the runtime acknowledges the request -
        it must NOT block until the agent finishes.  Poll with :meth:`status`.

        Args:
            request: Spawn parameters.

        Returns:
            Initial AgentStatus (typically state=PENDING or state=RUNNING).

        Raises:
            BridgeError: If the runtime rejects the spawn request.
        """
        ...

    @abstractmethod
    async def status(self, agent_id: str) -> AgentStatus:
        """Retrieve the current status of a running or completed agent.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Returns:
            Current AgentStatus.

        Raises:
            BridgeError: If the runtime cannot be reached or agent_id is
                         unknown.
        """
        ...

    @abstractmethod
    async def cancel(self, agent_id: str) -> None:
        """Request cancellation of a running agent.

        Best-effort - implementations should attempt a graceful shutdown first
        (SIGTERM) and escalate to a forceful kill after a short grace period.
        Cancelling an already-finished agent must not raise.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.

        Raises:
            BridgeError: If the runtime cannot be reached.
        """
        ...

    @abstractmethod
    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        """Fetch captured stdout/stderr from an agent run.

        Args:
            agent_id: Identifier originally supplied in SpawnRequest.
            tail: If given, return only the last *tail* lines.  If None,
                  return up to :attr:`config.max_log_bytes` bytes.

        Returns:
            Raw log bytes (UTF-8 encoded, newline-separated).

        Raises:
            BridgeError: If the runtime cannot be reached or logs are not
                         available.
        """
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier for this bridge implementation.

        Returns:
            Short lowercase string, e.g. ``"openclaw"``, ``"kubernetes"``.
        """
        ...


class BridgeError(Exception):
    """Raised when a RuntimeBridge call fails.

    Attributes:
        agent_id: The agent the error is associated with, if applicable.
        status_code: HTTP status code returned by the runtime, if applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.agent_id = agent_id
        self.status_code = status_code


class BridgeCapabilityError(BridgeError):
    """An optional bridge capability is unsupported, without legacy fallback."""

    def __init__(self, capability: str) -> None:
        _validate_remote_identity(capability, "capability")
        super().__init__(f"Runtime bridge does not support capability {capability!r}")
        self.capability = capability
