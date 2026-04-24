"""Registry that tracks active tunnels across provider drivers.

State is persisted to ``.sdd/runtime/tunnels.json`` with atomic writes
so a ``bernstein`` restart can still enumerate (and tear down) tunnels
it started earlier.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.tunnels.protocol import (
    ProviderNotAvailable,
    TunnelHandle,
    TunnelProvider,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# Preferred auto-detection order.
AUTO_ORDER: tuple[str, ...] = ("cloudflared", "bore", "ngrok", "tailscale")

STATE_PATH = Path(".sdd/runtime/tunnels.json")


def _atomic_write(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically via a tempfile rename.

    Args:
        path: Destination file path.
        data: String contents to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


class TunnelRegistry:
    """In-process registry of active tunnels with JSON state persistence.

    Args:
        state_path: Optional override of the state-file location.
        providers: Optional mapping of provider name to
            :class:`TunnelProvider`.  If ``None``, the registry is empty
            until drivers register themselves via :meth:`register`.
    """

    def __init__(
        self,
        state_path: Path | None = None,
        providers: Mapping[str, TunnelProvider] | None = None,
    ) -> None:
        """Initialize the registry and load any persisted state."""
        self._state_path = state_path or STATE_PATH
        self._providers: dict[str, TunnelProvider] = dict(providers or {})
        self._active: dict[str, TunnelHandle] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Populate :attr:`_active` from the state file if present."""
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = raw.get("tunnels", []) if isinstance(raw, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                handle = TunnelHandle(
                    name=str(item["name"]),
                    provider=str(item["provider"]),
                    port=int(item["port"]),
                    public_url=str(item["public_url"]),
                    pid=int(item["pid"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._active[handle.name] = handle

    def _save(self) -> None:
        """Persist :attr:`_active` atomically to the state file."""
        payload: dict[str, Any] = {
            "tunnels": [asdict(h) for h in self._active.values()],
        }
        _atomic_write(self._state_path, json.dumps(payload, indent=2) + "\n")

    # ------------------------------------------------------------------
    # Provider registration
    # ------------------------------------------------------------------

    def register(self, provider: TunnelProvider) -> None:
        """Register a provider driver under its :attr:`~TunnelProvider.name`.

        Args:
            provider: Driver instance to register.
        """
        self._providers[provider.name] = provider

    def providers(self) -> dict[str, TunnelProvider]:
        """Return a copy of the registered provider mapping.

        Returns:
            Dictionary of provider name to driver instance.
        """
        return dict(self._providers)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, name: str) -> TunnelHandle | None:
        """Return the active :class:`TunnelHandle` for ``name`` if any.

        Args:
            name: Tunnel name.

        Returns:
            The handle, or ``None`` if no tunnel by that name is active.
        """
        return self._active.get(name)

    def list_active(self) -> list[TunnelHandle]:
        """Return every currently-active tunnel handle.

        Returns:
            List of :class:`TunnelHandle` instances, one per active tunnel.
        """
        return list(self._active.values())

    # ------------------------------------------------------------------
    # Provider selection
    # ------------------------------------------------------------------

    def _pick_auto(self) -> TunnelProvider:
        """Pick the first registered provider whose binary is on PATH.

        Returns:
            The chosen driver instance.

        Raises:
            ProviderNotAvailable: If none of the known providers have
                their binaries installed.
        """
        tried: list[str] = []
        for name in AUTO_ORDER:
            prov = self._providers.get(name)
            if prov is None:
                continue
            if shutil.which(prov.binary) is not None:
                return prov
            tried.append(name)
        hint = "brew install cloudflared  # or: ngrok / bore / tailscale"
        raise ProviderNotAvailable(
            f"No tunnel binary found on PATH (tried: {', '.join(tried) or 'none'}).",
            hint=hint,
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def create(
        self,
        port: int,
        provider: str | None = None,
        name: str | None = None,
    ) -> TunnelHandle:
        """Start a new tunnel and persist it.

        Args:
            port: Local TCP port to expose.
            provider: Explicit provider name, or ``None`` / ``"auto"`` to
                auto-select the first available binary.
            name: Optional tunnel name; one is generated if omitted.

        Returns:
            A :class:`TunnelHandle` describing the started tunnel.

        Raises:
            KeyError: If ``provider`` is set but unknown.
            ProviderNotAvailable: If no provider binary is available.
        """
        if provider in (None, "auto"):
            drv = self._pick_auto()
        else:
            if provider not in self._providers:
                raise KeyError(f"Unknown tunnel provider: {provider}")
            drv = self._providers[provider]
        tunnel_name = name or f"{drv.name}-{uuid.uuid4().hex[:8]}"
        handle = drv.start(port, tunnel_name)
        self._active[handle.name] = handle
        self._save()
        return handle

    def destroy(self, name: str) -> bool:
        """Stop an active tunnel by name and remove it from the registry.

        Args:
            name: Tunnel name to destroy.

        Returns:
            ``True`` if a tunnel was stopped, ``False`` if none was found.
        """
        handle = self._active.pop(name, None)
        if handle is None:
            return False
        drv = self._providers.get(handle.provider)
        if drv is not None:
            with contextlib.suppress(Exception):
                drv.stop(name)
        self._save()
        return True
