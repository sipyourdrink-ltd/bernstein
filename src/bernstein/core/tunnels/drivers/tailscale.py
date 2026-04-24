"""Tailscale Funnel driver.

Wraps ``tailscale serve --bg --https=443 http://localhost:<port>`` and
reads the funnel URL out of ``tailscale status --json``.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from typing import Any

from bernstein.core.tunnels.protocol import (
    Detected,
    ProviderNotAvailable,
    TunnelHandle,
    TunnelProvider,
)


class TailscaleDriver(TunnelProvider):
    """Driver wrapping the ``tailscale`` binary (funnel mode)."""

    name = "tailscale"
    binary = "tailscale"

    def __init__(self) -> None:
        """Initialize an empty tunnel table."""
        self._active: dict[str, int] = {}

    def detect(self) -> Detected:
        """Probe for the tailscale binary.

        Returns:
            A :class:`Detected` describing the binary.

        Raises:
            ProviderNotAvailable: If the binary is missing or unrunnable.
        """
        path = shutil.which(self.binary)
        if path is None:
            raise ProviderNotAvailable(
                "tailscale is not installed.",
                hint="brew install tailscale",
            )
        try:
            res = subprocess.run(
                [path, "version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderNotAvailable(
                f"tailscale failed to run: {exc}",
                hint="brew install tailscale",
            ) from exc
        version = (res.stdout or res.stderr or "").strip().splitlines()[0] if (res.stdout or res.stderr) else ""
        return Detected(binary_path=path, version=version)

    @staticmethod
    def parse_url(status_json: str) -> str | None:
        """Extract the funnel URL from ``tailscale status --json``.

        Args:
            status_json: Raw JSON output from ``tailscale status --json``.

        Returns:
            The first ``https://`` funnel URL announced for the local
            node's DNS name, else ``None``.
        """
        try:
            obj: Any = json.loads(status_json)
        except ValueError:
            return None
        self_node = obj.get("Self") if isinstance(obj, dict) else None
        if not isinstance(self_node, dict):
            return None
        dns = self_node.get("DNSName")
        if isinstance(dns, str) and dns:
            return f"https://{dns.rstrip('.')}"
        return None

    def _get_funnel_url(self) -> str | None:
        """Shell out to ``tailscale status --json`` and parse the URL.

        Returns:
            The funnel URL or ``None`` if not determinable.
        """
        path = shutil.which(self.binary)
        if path is None:
            return None
        try:
            res = subprocess.run(
                [path, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return self.parse_url(res.stdout or "")

    def start(self, port: int, name: str) -> TunnelHandle:
        """Serve ``http://localhost:<port>`` via Tailscale Funnel.

        Args:
            port: Local TCP port to expose.
            name: Tunnel name.

        Returns:
            A :class:`TunnelHandle` describing the funnel URL.  Note: the
            underlying ``tailscale serve`` process is backgrounded by
            tailscaled itself, so ``pid`` reports the invoking CLI's PID
            (0 if unknown).

        Raises:
            ProviderNotAvailable: If the binary is missing.
            RuntimeError: If ``tailscale serve`` exits non-zero or no URL
                can be resolved.
        """
        self.detect()
        cmd = [
            self.binary,
            "serve",
            "--bg",
            "--https=443",
            f"http://localhost:{port}",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"tailscale serve failed: {exc}") from exc
        if res.returncode != 0:
            raise RuntimeError(f"tailscale serve exited {res.returncode}: {(res.stderr or res.stdout or '').strip()}")
        url = self._get_funnel_url()
        if url is None:
            raise RuntimeError("could not resolve tailscale funnel URL")
        pid = 0  # tailscaled owns the listener; we don't track a child pid
        self._active[name] = pid
        return TunnelHandle(
            name=name,
            provider=self.name,
            port=port,
            public_url=url,
            pid=pid,
        )

    def stop(self, name: str) -> None:
        """Tear down the tailscale funnel for ``name``.

        Args:
            name: Tunnel name previously passed to :meth:`start`.
        """
        if name not in self._active:
            return
        self._active.pop(name, None)
        path = shutil.which(self.binary)
        if path is None:
            return
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [path, "serve", "reset"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
