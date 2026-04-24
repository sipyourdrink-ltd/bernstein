"""Abstract tunnel provider protocol.

Defines the common surface every tunnel driver must implement.  Drivers
wrap a local binary (cloudflared, ngrok, bore, tailscale) and expose a
start/stop/detect interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Detected:
    """Result of a successful binary detection.

    Attributes:
        binary_path: Absolute path to the detected binary on PATH.
        version: Version string reported by ``<binary> --version``.
    """

    binary_path: str
    version: str


@dataclass(frozen=True)
class TunnelHandle:
    """A live tunnel owned by a provider.

    Attributes:
        name: Caller-supplied (or auto-generated) tunnel name.
        provider: Provider name (e.g. ``"cloudflared"``).
        port: Local port being exposed.
        public_url: Publicly reachable URL for the tunnel.
        pid: OS process id of the spawned tunnel binary.
    """

    name: str
    provider: str
    port: int
    public_url: str
    pid: int


class ProviderNotAvailable(RuntimeError):
    """Raised when a provider's binary is not installed or not runnable.

    Attributes:
        hint: Human-readable install command (e.g. ``"brew install bore"``).
    """

    def __init__(self, message: str, *, hint: str) -> None:
        """Initialize with a message and install hint.

        Args:
            message: Human-readable error message.
            hint: Suggested install command.
        """
        super().__init__(message)
        self.hint = hint


class TunnelProvider(ABC):
    """Abstract base class for tunnel providers.

    Each concrete subclass shells out to a specific local binary and
    parses its stdout to discover the public URL.
    """

    name: str = ""
    binary: str = ""

    @abstractmethod
    def detect(self) -> Detected:
        """Verify the provider's binary is installed and runnable.

        Returns:
            A :class:`Detected` describing the binary path and version.

        Raises:
            ProviderNotAvailable: If the binary is not on PATH or the
                version probe fails.
        """

    @abstractmethod
    def start(self, port: int, name: str) -> TunnelHandle:
        """Start a tunnel for the given local port.

        Args:
            port: The local TCP port to expose.
            name: Caller-supplied tunnel name (used as a registry key).

        Returns:
            A :class:`TunnelHandle` for the newly started tunnel.

        Raises:
            ProviderNotAvailable: If the binary is missing.
            RuntimeError: If the provider fails to emit a public URL.
        """

    @abstractmethod
    def stop(self, name: str) -> None:
        """Stop a previously started tunnel by name.

        Args:
            name: The tunnel name passed to :meth:`start`.
        """
