"""Guarded process termination for persisted tunnel handles.

Persisted tunnel state in ``.sdd/runtime/tunnels.json`` stores an integer PID,
which is neither an exclusive capability nor a durable process identity.  Every
tunnel teardown path (``gui/cli.py``, ``tunnel_cmd.py``, ``stop_cmd.py``, and
``preview/tunnel_bridge.py``) routes through :func:`terminate_tunnel_process`
so that:

* non-process / special POSIX PIDs (``bool``, ``<= 1`` including ``0``, ``-1``,
  process-group negative targets, and init/container PID ``1``), our own PID
  (``os.getpid()``), and our own process-group leader (``os.getpgrp()``) are
  never signaled;
* providers that manage no child daemon (``tailscale``) are never signaled;
* dead or zombie processes are skipped via :func:`is_process_alive`; and
* live PIDs whose command line can be inspected on POSIX and does not match the
  tunnel provider binary (recycled PID) are left untouched.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
from typing import TYPE_CHECKING, Final

from bernstein.core.orchestration.process_utils import (
    cmdline_matches,
    is_process_alive,
    pid_command_line,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.tunnels.protocol import TunnelHandle, TunnelProvider

logger = logging.getLogger(__name__)

#: Providers that manage tunnels out-of-process (no child daemon PID to kill).
_NO_SIGNAL_PROVIDERS: Final[frozenset[str]] = frozenset({"tailscale"})

#: Default binary markers for shipped stream-process tunnel drivers.
_KNOWN_PROVIDER_BINARIES: Final[dict[str, tuple[str, ...]]] = {
    "cloudflared": ("cloudflared",),
    "bore": ("bore",),
    "ngrok": ("ngrok",),
}


def is_signallable_tunnel_pid(pid: object) -> bool:
    """Return ``True`` only for a regular PID that is safe to target.

    Rejects ``bool`` (``bool`` subclasses ``int``), ``<= 1`` (init/container
    PID 1, process-group 0, broadcast -1, negative PGIDs), the current process
    PID, and (on POSIX) the current process-group leader PID.
    """
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    if pid <= 1:
        return False
    if pid == os.getpid():
        return False
    if hasattr(os, "getpgrp"):
        with contextlib.suppress(OSError):
            if pid == os.getpgrp():
                return False
    return True


def expected_tunnel_markers(
    handle: TunnelHandle,
    providers: Mapping[str, TunnelProvider] | None = None,
) -> tuple[str, ...]:
    """Return expected command-line markers for *handle*'s tunnel binary."""
    markers: list[str] = []
    provider_name = (handle.provider or "").strip()
    if providers is not None and provider_name in providers:
        binary = getattr(providers[provider_name], "binary", "")
        if isinstance(binary, str) and binary.strip():
            markers.append(binary.strip())
    elif provider_name in _KNOWN_PROVIDER_BINARIES:
        markers.extend(_KNOWN_PROVIDER_BINARIES[provider_name])
    if provider_name and provider_name not in markers:
        markers.append(provider_name)
    return tuple(markers)


def terminate_tunnel_process(
    handle: TunnelHandle,
    *,
    providers: Mapping[str, TunnelProvider] | None = None,
    sig: int = signal.SIGTERM,
) -> bool:
    """Send *sig* to *handle*'s tunnel process after PID and identity checks.

    Returns ``True`` if ``os.kill`` succeeded, or ``False`` when signaling was
    refused by a guard, the process was not alive, its command line did not
    match the tunnel binary, or ``os.kill`` raised ``OSError``.
    """
    if handle.provider in _NO_SIGNAL_PROVIDERS:
        return False
    if not is_signallable_tunnel_pid(handle.pid):
        return False
    markers = expected_tunnel_markers(handle, providers)
    if not markers:
        return False
    if not is_process_alive(handle.pid):
        return False
    cmdline = pid_command_line(handle.pid)
    if cmdline is not None and not cmdline_matches(cmdline, markers):
        logger.debug(
            "Skipping signal for tunnel %s: pid %d cmdline %r does not match %r",
            handle.name,
            handle.pid,
            cmdline,
            markers,
        )
        return False
    try:
        # PID passed non-process/self/pgrp, liveness, and binary identity
        # checks above (Sonar python:S4828).
        os.kill(handle.pid, sig)  # NOSONAR python:S4828
    except OSError as exc:
        logger.debug("Signal %s to tunnel %s (pid %d) failed: %s", sig, handle.name, handle.pid, exc)
        return False
    return True


__all__ = [
    "expected_tunnel_markers",
    "is_signallable_tunnel_pid",
    "terminate_tunnel_process",
]
