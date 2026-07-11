"""A faithful in-process double for the ssh transport (issue #2352, AC4).

The double stands in for the *external ssh boundary only*: the network hop
and the ``ssh`` binary. Every other layer of
:class:`~bernstein.core.sandbox.ssh_backend.SSHSandboxBackend` runs for real
against it -- base64 file transfer, ``cd``/``env`` command composition, and the
remote ``git worktree`` lifecycle all execute as genuine shell commands on the
local host, rooted at whatever absolute remote paths the backend chose. That is
what makes the double faithful rather than a stub: the production code path is
exercised end to end; only the transport hop is replaced.

The backend hands the transport a fully-built ``ssh`` argv. For an exec/popen
call the remote program is always the last argv element (``sh -c '<script>'``),
so the double runs exactly that string through the local shell. Control-plane
argvs (``-fN -M`` to open the ControlMaster, ``-O exit`` to close it) carry no
remote program, so the double treats them as no-op successes -- there is no
multiplexed socket to open when there is no wire.
"""

from __future__ import annotations

import asyncio
import subprocess

from bernstein.core.sandbox.ssh_backend import RemoteExec


class InProcessSSHTransport:
    """Run the remote side of every ssh call in-process, on the local host.

    Attributes:
        commands: Every remote program string handed to :meth:`run_async`
            or :meth:`popen`, in call order. Tests assert against these to
            prove which worktree each command touched.
        master_opens: Count of ControlMaster opens requested.
        master_closes: Count of ControlMaster closes requested.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.master_opens = 0
        self.master_closes = 0

    def run_blocking(self, argv: list[str]) -> RemoteExec:
        """Handle a control-plane ssh call (open / close the multiplex socket)."""
        if "-O" in argv:
            self.master_closes += 1
        else:
            self.master_opens += 1
        return RemoteExec(returncode=0, stdout=b"", stderr=b"")

    async def run_async(
        self,
        argv: list[str],
        *,
        timeout_seconds: int | None = None,
        stdin: bytes | None = None,
    ) -> RemoteExec:
        """Execute the remote program (argv[-1]) through the local shell."""
        remote_program = argv[-1]
        self.commands.append(remote_program)
        process = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            remote_program,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate(input=stdin)
        return RemoteExec(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=out,
            stderr=err,
        )

    def popen(
        self,
        argv: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        """Spawn the remote program (argv[-1]) as a local shell subprocess."""
        remote_program = argv[-1]
        self.commands.append(remote_program)
        return subprocess.Popen(
            ["sh", "-c", remote_program],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )


__all__ = ["InProcessSSHTransport"]
