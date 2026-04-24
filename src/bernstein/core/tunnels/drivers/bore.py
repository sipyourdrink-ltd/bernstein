"""bore.pub driver.

Runs ``bore local <port> --to bore.pub`` and parses ``listening at
bore.pub:<PORT>`` from stdout to build the public URL.
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

_LISTEN_RE = re.compile(r"listening at\s+(?P<host>[a-zA-Z0-9.-]+):(?P<port>\d+)")
_START_TIMEOUT_S = 30.0


class BoreDriver(TunnelProvider):
    """Driver wrapping the ``bore`` binary against ``bore.pub``."""

    name = "bore"
    binary = "bore"

    def __init__(self) -> None:
        """Initialize with an empty process table."""
        self._procs: dict[str, subprocess.Popen[str]] = {}

    def detect(self) -> Detected:
        """Probe for the bore binary.

        Returns:
            A :class:`Detected` describing the binary.

        Raises:
            ProviderNotAvailable: If the binary is missing or unrunnable.
        """
        path = shutil.which(self.binary)
        if path is None:
            raise ProviderNotAvailable(
                "bore is not installed.",
                hint="cargo install bore-cli",
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
                f"bore failed to run: {exc}",
                hint="cargo install bore-cli",
            ) from exc
        version = (res.stdout or res.stderr or "").strip().splitlines()[0] if (res.stdout or res.stderr) else ""
        return Detected(binary_path=path, version=version)

    @staticmethod
    def parse_url(output: str) -> str | None:
        """Build the public URL from a bore ``listening at`` line.

        Args:
            output: Captured stdout text.

        Returns:
            ``tcp://<host>:<port>`` if a listen line is found, else
            ``None``.
        """
        match = _LISTEN_RE.search(output)
        if match is None:
            return None
        return f"tcp://{match.group('host')}:{match.group('port')}"

    def start(self, port: int, name: str) -> TunnelHandle:
        """Start a bore tunnel for ``port``.

        Args:
            port: Local TCP port.
            name: Tunnel name.

        Returns:
            A :class:`TunnelHandle` once the remote port is announced.

        Raises:
            ProviderNotAvailable: If the binary is missing.
            RuntimeError: If bore does not announce a port in time.
        """
        self.detect()
        cmd = [self.binary, "local", str(port), "--to", "bore.pub"]
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
        self.stop(name)
        raise RuntimeError("bore did not announce a remote port in time")

    def stop(self, name: str) -> None:
        """Terminate the bore tunnel process for ``name``.

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
