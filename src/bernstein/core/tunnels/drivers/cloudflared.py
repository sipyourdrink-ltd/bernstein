"""Cloudflared quick-tunnel driver.

Shells out to ``cloudflared tunnel --url http://localhost:<port>`` and
parses the ``https://*.trycloudflare.com`` URL from stdout/stderr.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import signal
import subprocess
import time

from bernstein.core.tunnels.protocol import (
    Detected,
    ProviderNotAvailable,
    TunnelHandle,
    TunnelProvider,
)

_URL_RE = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")
_START_TIMEOUT_S = 30.0


class CloudflaredDriver(TunnelProvider):
    """Driver wrapping the ``cloudflared`` binary."""

    name = "cloudflared"
    binary = "cloudflared"

    def __init__(self) -> None:
        """Initialize the driver with an empty process table."""
        self._procs: dict[str, subprocess.Popen[str]] = {}

    def detect(self) -> Detected:
        """Probe for the cloudflared binary.

        Returns:
            A :class:`Detected` describing the binary.

        Raises:
            ProviderNotAvailable: If the binary is missing or unrunnable.
        """
        path = shutil.which(self.binary)
        if path is None:
            raise ProviderNotAvailable(
                "cloudflared is not installed.",
                hint="brew install cloudflared",
            )
        try:
            res = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderNotAvailable(
                f"cloudflared failed to run: {exc}",
                hint="brew install cloudflared",
            ) from exc
        version = (res.stdout or res.stderr or "").strip().splitlines()[0] if (res.stdout or res.stderr) else ""
        return Detected(binary_path=path, version=version)

    @staticmethod
    def parse_url(output: str) -> str | None:
        """Parse the public trycloudflare URL from captured output.

        Args:
            output: Accumulated stdout/stderr text from ``cloudflared``.

        Returns:
            The public URL if present, else ``None``.
        """
        match = _URL_RE.search(output)
        return match.group(0) if match else None

    def start(self, port: int, name: str) -> TunnelHandle:
        """Start a cloudflared quick tunnel for ``port``.

        Args:
            port: Local TCP port.
            name: Tunnel name.

        Returns:
            A :class:`TunnelHandle` once the public URL is observed.

        Raises:
            ProviderNotAvailable: If the binary is missing.
            RuntimeError: If no URL is printed within the startup window.
        """
        self.detect()
        cmd = [self.binary, "tunnel", "--url", f"http://localhost:{port}"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._procs[name] = proc

        assert proc.stdout is not None
        deadline = time.monotonic() + _START_TIMEOUT_S
        buf: list[str] = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            buf.append(line)
            url = self.parse_url("".join(buf))
            if url:
                return TunnelHandle(
                    name=name,
                    provider=self.name,
                    port=port,
                    public_url=url,
                    pid=proc.pid,
                )
        # Give up — kill the process and raise.
        self.stop(name)
        raise RuntimeError("cloudflared did not emit a public URL in time")

    def stop(self, name: str) -> None:
        """Terminate the tunnel process for ``name``.

        Args:
            name: Tunnel name.
        """
        proc = self._procs.pop(name, None)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                proc.kill()
