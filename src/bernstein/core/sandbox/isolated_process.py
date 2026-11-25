"""Bounded OCI processes for code with no authority over the host."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class IsolationUnavailable(RuntimeError):
    """The isolated process could not be started."""


def runtime_executable() -> str:
    """Resolve a local supported OCI runtime without a host-exec fallback."""
    executable = shutil.which("docker") or shutil.which("podman")
    if executable is None:
        raise IsolationUnavailable("no isolated runtime available; install Docker or Podman")
    return executable


def readonly_bind(source: Path, destination: str) -> str:
    """Build one literal OCI mount specification for a known destination."""
    path = str(source.resolve())
    if any(character in path for character in (",", '"', "\n", "\r")):
        raise IsolationUnavailable("workspace path contains characters unsupported by the isolated runtime")
    return f"type=bind,source={path},target={destination},readonly"


def run_isolated(
    image: str,
    argv: list[str],
    *,
    input_data: bytes,
    timeout: int,
    mounts: tuple[str, ...] = (),
    workdir: str = "/tmp",
) -> subprocess.CompletedProcess[bytes]:
    """Run a bounded foreground job with an explicit image and mounts."""
    if not image or image.startswith("-"):
        raise IsolationUnavailable("invalid isolated runtime image")
    runtime = runtime_executable()
    name = f"bernstein-isolated-{uuid.uuid4().hex}"
    command = [
        runtime,
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--interactive",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--memory=256m",
        "--memory-swap=256m",
        "--cpus=1",
        "--pids-limit=32",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=16m",
        "--ulimit=fsize=1048576:1048576",
        "--log-driver=none",
        f"--workdir={workdir}",
    ]
    for mount in mounts:
        command.extend(("--mount", mount))
    command.extend(("--", image, *argv))
    try:
        with tempfile.TemporaryFile() as source:
            source.write(input_data)
            source.seek(0)
            with subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                assert process.stdout is not None
                stream = process.stdout
                output: list[bytes] = []
                reader = threading.Thread(target=lambda: output.append(stream.read(65537)), daemon=True)
                reader.start()
                deadline = time.monotonic() + timeout
                reader.join(timeout)
                if reader.is_alive() or not output:
                    process.kill()
                    raise subprocess.TimeoutExpired(command, timeout)
                data = output[0]
                if len(data) > 65536:
                    process.kill()
                    raise IsolationUnavailable("isolated process result exceeds the output limit")
                try:
                    returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    raise
        if returncode in (125, 126, 127):
            raise IsolationUnavailable("isolated runtime or its local image could not start the process")
        return subprocess.CompletedProcess(command, returncode, data, b"")
    except OSError as exc:
        raise IsolationUnavailable("could not launch the isolated runtime") from exc
    finally:
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [runtime, "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
