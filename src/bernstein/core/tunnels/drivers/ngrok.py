"""ngrok driver.

Runs ``ngrok http <port> --log stdout --log-format json`` and parses
the first ``url=https://...`` field out of the JSON log stream.
"""

from __future__ import annotations

import contextlib
import json
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

_START_TIMEOUT_S = 30.0


class NgrokDriver(TunnelProvider):
    """Driver wrapping the ``ngrok`` binary."""

    name = "ngrok"
    binary = "ngrok"

    def __init__(self) -> None:
        """Initialize with an empty process table."""
        self._procs: dict[str, subprocess.Popen[str]] = {}

    def detect(self) -> Detected:
        """Probe for the ngrok binary.

        Returns:
            A :class:`Detected` describing the binary.

        Raises:
            ProviderNotAvailable: If the binary is missing or unrunnable.
        """
        path = shutil.which(self.binary)
        if path is None:
            raise ProviderNotAvailable(
                "ngrok is not installed.",
                hint="brew install ngrok/ngrok/ngrok",
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
                f"ngrok failed to run: {exc}",
                hint="brew install ngrok/ngrok/ngrok",
            ) from exc
        version = (res.stdout or res.stderr or "").strip().splitlines()[0] if (res.stdout or res.stderr) else ""
        return Detected(binary_path=path, version=version)

    @staticmethod
    def parse_url(output: str) -> str | None:
        """Extract the public URL from an ngrok JSON log stream.

        ngrok writes one JSON object per line; the tunnel-started event
        includes a ``url`` key with the public https URL.

        Args:
            output: Captured stdout text.

        Returns:
            The public URL if a ``url`` field is found, else ``None``.
        """
        for line in output.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            url = obj.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
        return None

    def start(self, port: int, name: str) -> TunnelHandle:
        """Start an ngrok tunnel for ``port``.

        Args:
            port: Local TCP port.
            name: Tunnel name.

        Returns:
            A :class:`TunnelHandle` once the URL is captured.

        Raises:
            ProviderNotAvailable: If the binary is missing.
            RuntimeError: If no URL is emitted in time.
        """
        self.detect()
        cmd = [
            self.binary,
            "http",
            str(port),
            "--log",
            "stdout",
            "--log-format",
            "json",
        ]
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
        raise RuntimeError("ngrok did not emit a public URL in time")

    def stop(self, name: str) -> None:
        """Terminate the ngrok tunnel process for ``name``.

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
