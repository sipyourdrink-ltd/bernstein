"""Adapter from the ``bernstein preview`` flow to the existing tunnel wrapper.

The bridge exposes a tiny, test-friendly surface around the
:class:`~bernstein.core.tunnels.registry.TunnelRegistry` so the
:class:`~bernstein.core.preview.manager.PreviewManager` doesn't have to
construct registries, register drivers, or know how to fall back from
``provider=auto`` to ``provider=cloudflared``.

The bridge never reimplements tunnel behaviour — every call funnels
into the existing wrapper.
"""

from __future__ import annotations

import logging
import os
import signal
from typing import TYPE_CHECKING

from bernstein.core.tunnels.drivers import register_default_drivers
from bernstein.core.tunnels.protocol import (
    ProviderNotAvailable,
    TunnelHandle,
)
from bernstein.core.tunnels.registry import TunnelRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


class TunnelBridgeError(RuntimeError):
    """Raised when the bridge cannot open a tunnel through any provider."""


class TunnelBridge:
    """Thin facade over :class:`TunnelRegistry`.

    Args:
        state_path: Optional override for the tunnel registry's state
            file. Tests should supply a temp path; production callers
            should leave this as ``None`` to share state with
            ``bernstein tunnel``.
        registry_factory: Optional factory used to build the registry —
            mostly so tests can substitute fake providers.
    """

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        registry_factory: Callable[[], TunnelRegistry] | None = None,
    ) -> None:
        if registry_factory is None:

            def _default_factory() -> TunnelRegistry:
                reg = TunnelRegistry(state_path=state_path)
                register_default_drivers(reg)
                return reg

            self._factory = _default_factory
        else:
            self._factory = registry_factory

    def _build(self) -> TunnelRegistry:
        return self._factory()

    def open(
        self,
        *,
        port: int,
        provider: str = "auto",
        name: str | None = None,
    ) -> TunnelHandle:
        """Open a tunnel for *port*.

        Tries the requested *provider* first; when ``provider="auto"``
        and no binary is on PATH, the bridge re-tries with
        ``provider="cloudflared"`` because the ticket pins that as the
        explicit fallback.

        Args:
            port: Local port to expose.
            provider: Tunnel provider (``"auto"`` by default).
            name: Optional tunnel name. The registry generates one when
                omitted.

        Returns:
            A :class:`TunnelHandle` describing the live tunnel.

        Raises:
            TunnelBridgeError: When no provider could open the tunnel.
        """
        reg = self._build()
        try:
            return reg.create(port=port, provider=provider, name=name)
        except ProviderNotAvailable as primary_exc:
            logger.warning("Primary tunnel provider unavailable: %s", primary_exc)
            if provider == "auto":
                # Retry with the explicit ticket-mandated fallback.
                try:
                    return reg.create(port=port, provider="cloudflared", name=name)
                except ProviderNotAvailable as fallback_exc:
                    raise TunnelBridgeError(
                        f"No tunnel provider available (auto + cloudflared fallback). Hint: {fallback_exc.hint}"
                    ) from fallback_exc
                except Exception as fallback_exc:
                    raise TunnelBridgeError(
                        f"Tunnel start failed via cloudflared fallback: {fallback_exc}"
                    ) from fallback_exc
            raise TunnelBridgeError(
                f"Tunnel provider {provider!r} unavailable: {primary_exc}. Hint: {primary_exc.hint}"
            ) from primary_exc
        except KeyError as exc:
            raise TunnelBridgeError(f"Unknown tunnel provider: {provider!r}") from exc
        except Exception as exc:
            raise TunnelBridgeError(f"Tunnel start failed: {exc}") from exc

    def close(self, name: str) -> bool:
        """Tear down the named tunnel via the registry.

        Sends ``SIGTERM`` to the process the registry recorded so
        provider binaries that don't honour ``stop`` still go down.

        Args:
            name: Tunnel name returned by :meth:`open`.

        Returns:
            ``True`` if a tunnel was found and stopped; ``False`` if
            no record matched.
        """
        reg = self._build()
        handle = reg.get(name)
        if handle is None:
            return False
        if handle.pid > 0:
            try:
                # We only ever SIGTERM PIDs we wrote into tunnels.json
                # ourselves (Sonar python:S4828).
                os.kill(handle.pid, signal.SIGTERM)  # NOSONAR python:S4828
            except OSError as exc:
                logger.debug("SIGTERM to tunnel pid %d failed: %s", handle.pid, exc)
        return reg.destroy(name)

    def list(self) -> list[TunnelHandle]:
        """Return every tunnel currently tracked by the registry."""
        return self._build().list_active()


__all__ = [
    "TunnelBridge",
    "TunnelBridgeError",
]
