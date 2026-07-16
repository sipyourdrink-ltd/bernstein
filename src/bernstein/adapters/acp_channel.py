"""Adapter-side binding for the ACP event channel (#2522).

An adapter whose upstream CLI speaks the Agent Client Protocol declares
:attr:`bernstein.adapters._contract.EventChannel.ACP`. Instead of tailing
stdout for a bespoke ``BERNSTEIN:`` text grammar or a per-CLI JSON dialect,
it binds the upstream process's line-delimited JSON-RPC stream onto the
content-addressed client transport in
:mod:`bernstein.core.protocols.acp.client`.

The lifecycle is driven entirely from structured, schema-validated frames:
there is no text parser in this path, so an upstream output-format change
cannot drift it. Every inbound event is journaled content-addressed, so the
run's replay identity covers the agent's output.

This module is a shared helper (like :mod:`bernstein.adapters.base`), not a
per-CLI adapter, so it sits outside the adapters-independence contract and
may be imported by any adapter that declares the ACP channel.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bernstein.adapters._contract import EventChannel, strategy_for
from bernstein.core.protocols.acp.client import (
    ACPEventJournalSink,
    AcpLifecycleResult,
    drive_acp_lifecycle,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal

__all__ = [
    "AcpLifecycleResult",
    "adapter_speaks_acp",
    "iter_process_frames",
    "run_acp_channel",
]


def adapter_speaks_acp(adapter_name: str) -> bool:
    """Return whether *adapter_name* declares the ACP event channel.

    Accepts either a registry key (``"kilo"``) or the session-namespace
    form; resolution goes through :func:`strategy_for`, so an undeclared
    adapter answers ``False`` (its conservative default channel is text
    signals).
    """
    return strategy_for(adapter_name).event_channel is EventChannel.ACP


def run_acp_channel(
    inbound: Iterable[bytes | str],
    *,
    journal: EventJournal,
    session_id: str,
    stop_at_terminal: bool = True,
) -> AcpLifecycleResult:
    """Drive an ACP-speaking adapter's lifecycle from an inbound frame stream.

    Binds the upstream agent's JSON-RPC frames onto the content-addressed
    journal and returns a lifecycle result the monitoring layer can map onto
    a terminal state - with zero stdout/stderr lifecycle parsing.

    Args:
        inbound: The upstream agent's stdout as raw JSON-RPC frame lines
            (for a live process, :func:`iter_process_frames`).
        journal: The run's event journal; every inbound ACP event is
            recorded content-addressed.
        session_id: The adapter session id, retained for symmetry with the
            text-signal monitoring surface and for future correlation.
        stop_at_terminal: Stop after the first terminal ACP event.

    Returns:
        An :class:`AcpLifecycleResult`.

    Raises:
        ACPSchemaError: An inbound frame failed schema validation. The
            malformed frame is refused at the boundary and journals nothing.
    """
    del session_id  # retained for API symmetry; journal already namespaces the run
    sink = ACPEventJournalSink(journal)
    return drive_acp_lifecycle(inbound, sink, stop_at_terminal=stop_at_terminal)


def iter_process_frames(proc: subprocess.Popen[bytes]) -> Iterator[bytes]:
    """Yield line-delimited JSON-RPC frames from a live ACP subprocess.

    The upstream CLI is spawned with ``stdout=PIPE`` and speaks
    line-delimited JSON-RPC (one frame per line). This generator yields each
    line as it arrives so :func:`run_acp_channel` can validate and journal it
    incrementally.

    Args:
        proc: A :class:`subprocess.Popen` opened in binary mode with a piped
            stdout.

    Yields:
        Each non-empty stdout line, including its trailing newline.
    """
    if proc.stdout is None:  # pragma: no cover - defensive
        return
    for line in proc.stdout:
        if line.strip():
            yield line


def spawn_acp_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    """Spawn an upstream CLI as an ACP subprocess with a piped JSON-RPC stdout.

    The process's stdout is a pipe (line-delimited JSON-RPC frames the client
    transport reads); stderr is redirected to *log_path* for operator
    diagnostics. The caller owns draining stdout via
    :func:`iter_process_frames` and reaping the process.

    Args:
        cmd: The upstream CLI invocation (already wrapped as needed).
        cwd: Working directory for the process.
        env: The filtered environment for the process.
        log_path: File to capture the process's stderr into.

    Returns:
        The spawned :class:`subprocess.Popen`.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_file = log_path.open("wb")
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        start_new_session=True,
    )
