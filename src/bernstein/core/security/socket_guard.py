"""Process-wide runtime socket guard for ``--profile airgap``.

The :mod:`bernstein.core.security.network_policy` module gates *known*
adapter endpoints at registration time. That covers the documented
SaaS surfaces (Anthropic / OpenAI / Google / Cloudflare) but does
NOT stop a misbehaving plugin or library from opening an arbitrary
socket the orchestrator never advertised. Sovereign customers expect
``--profile airgap`` to be a hard fail-closed boundary -- so this
module installs a process-wide hook on
:class:`socket.socket.connect` that consults the active policy on
every TCP/UDP connect attempt.

Design notes:

* The guard is **opt-in**. It only patches when the airgap profile
  is active (``BERNSTEIN_PROFILE_MODE=airgap``). Outside the profile
  the back-compat default is allow-all and patching would be
  surprising / break tooling that relies on legacy behaviour.
* DNS still works -- ``getaddrinfo`` is a separate syscall path. If
  the policy denies the *resolved* host, we raise on ``connect``
  rather than on resolution. This matches the stated semantics in
  :mod:`network_policy` ("DNS queries route to a host, the host
  is the one that gets policy-checked").
* AF_UNIX sockets are exempt -- they never leave the machine and
  legitimate IPC (gRPC over UDS, journald) would otherwise break.
* The patched ``connect`` accepts both ``connect((host, port))``
  and the IPv6 4-tuple form ``(host, port, flowinfo, scopeid)``.
* The guard is idempotent: ``install_runtime_socket_guard()`` can
  be called many times; only the first call patches the global.
* Install and uninstall key off the *identity* of the callable on
  the class, not off a bookkeeping flag. Bernstein is not the only
  thing that may patch ``socket.socket.connect``, and a flag that
  outlives the patch it describes would otherwise let uninstall
  write a dead wrapper into the process -- or let install report an
  egress boundary it never put in place.
"""

from __future__ import annotations

import contextlib
import logging
import socket
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

from bernstein.core.security.network_policy import (
    ENV_PROFILE_MODE,
    PROFILE_AIRGAP,
    NetworkPolicyDenied,
    is_airgap_profile,
    policy_from_env,
)

logger = logging.getLogger(__name__)

_INSTALLED_FLAG: Final[str] = "_bernstein_socket_guard_installed"
_ORIGINAL_FLAG: Final[str] = "_bernstein_socket_guard_original_connect"
_GUARD_FLAG: Final[str] = "_bernstein_socket_guard_active_connect"

__all__ = [
    "ENV_PROFILE_MODE",
    "PROFILE_AIRGAP",
    "install_runtime_socket_guard",
    "is_runtime_socket_guard_installed",
    "uninstall_runtime_socket_guard",
]


def _extract_host_port(address: Any) -> tuple[str, int | None] | None:
    """Best-effort decode of the ``connect`` address argument.

    Handles:
    - ``(host, port)`` for AF_INET
    - ``(host, port, flowinfo, scopeid)`` for AF_INET6
    - ``str`` / ``bytes`` for AF_UNIX (returns ``None`` to skip the check)

    Returns ``None`` when the address shape is not understood; the
    caller treats that as "let the original connect decide". We do
    NOT want a parse error here to be a silent bypass of the guard,
    but we also do not want the guard to crash exotic socket usage
    that pre-dates IPv6.
    """
    if isinstance(address, (str, bytes)):
        return None
    if not isinstance(address, tuple) or not address:
        return None
    host = address[0]
    port = address[1] if len(address) >= 2 else None
    if not isinstance(host, str):
        return None
    if port is not None and not isinstance(port, int):
        return None
    return host, port


def _is_unix_socket(sock: socket.socket) -> bool:
    """Return True for AF_UNIX sockets (always exempt).

    ``socket.AF_UNIX`` does not exist on Windows. Guard the lookup so
    the runtime test of the airgap profile still passes there.
    """
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is None:
        return False
    return sock.family == af_unix


def _make_guarded_connect(original: Any) -> Any:
    """Wrap the original ``socket.socket.connect`` with the policy gate.

    Closures keep the original around so :func:`uninstall_runtime_socket_guard`
    can restore it for tests that need the unpatched primitive.
    """

    def _guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_unix_socket(self):
            return original(self, address, *args, **kwargs)
        decoded = _extract_host_port(address)
        if decoded is None:
            return original(self, address, *args, **kwargs)
        host, port = decoded
        # Re-read the policy each call: subprocess code may have toggled
        # BERNSTEIN_PROFILE_MODE between the initial install and now.
        if not is_airgap_profile():
            return original(self, address, *args, **kwargs)
        policy = policy_from_env()
        if policy.is_allowed(host, port):
            return original(self, address, *args, **kwargs)
        dest = f"{host}:{port}" if port is not None else host
        logger.warning(
            "airgap runtime guard refused socket.connect to %s (policy: %s)",
            dest,
            policy.to_env_value(),
        )
        raise NetworkPolicyDenied(dest, source="socket-guard")

    return _guarded_connect


def _guard_is_live(sock_cls: type[socket.socket]) -> bool:
    """Return True iff *our* guard is the callable currently on the class.

    The ``_INSTALLED_FLAG`` alone is not enough. Bernstein is not the only
    thing that patches ``socket.socket.connect``: a test harness, tracer, or
    sandbox shim may swap in its own wrapper and restore its predecessor when
    its scope ends. If the guard was installed while such a shim was live and
    was not uninstalled before the shim went away, the flags describe an
    installation that no longer exists and ``_ORIGINAL_FLAG`` holds a callable
    nothing references any more. Acting on the flags then writes that dead
    wrapper onto the class process-wide. Compare identities instead.
    """
    if not getattr(sock_cls, _INSTALLED_FLAG, False):
        return False
    guard = getattr(sock_cls, _GUARD_FLAG, None)
    return guard is not None and sock_cls.connect is guard


def _clear_guard_state(sock_cls: type[socket.socket]) -> None:
    """Drop the bookkeeping without touching ``connect``."""
    setattr(sock_cls, _INSTALLED_FLAG, False)
    for flag in (_ORIGINAL_FLAG, _GUARD_FLAG):
        with contextlib.suppress(AttributeError):
            delattr(sock_cls, flag)


def install_runtime_socket_guard(*, force: bool = False) -> bool:
    """Install the process-wide runtime egress hook.

    The guard wraps :class:`socket.socket.connect` so every outbound
    TCP/UDP attempt is run past the active network policy. AF_UNIX
    sockets are exempt. Loopback (``127.0.0.1`` / ``::1``) is allowed
    only if the operator added them via ``--allow-network`` or runs
    outside the airgap profile.

    Args:
        force: When True, reinstall even if a previous installation
            exists. Used by tests that need to swap in a fresh
            original to capture monkeypatched state.

    Returns:
        True if the guard was installed (or already installed) and
        is now active. False if the airgap profile is not active and
        ``force`` is not set -- the guard is a no-op outside airgap
        mode and a quiet decline keeps callsites simple.
    """
    if not force and not is_airgap_profile():
        return False
    sock_cls = socket.socket
    live = _guard_is_live(sock_cls)
    if live and not force:
        return True
    if live:
        # Restore first so we close over the truly original connect,
        # not over the previous guard.
        sock_cls.connect = getattr(sock_cls, _ORIGINAL_FLAG, sock_cls.connect)  # type: ignore[method-assign]
    # Every other state -- never installed, or flags left over from an
    # installation another patcher has since displaced -- is rebuilt from the
    # live ``connect`` below, and the stale original is dropped rather than
    # restored. The displaced case must not short-circuit as "already
    # installed": reporting an egress boundary that is not actually patched in
    # is the one failure mode an airgap run cannot tolerate.
    original_connect = sock_cls.connect
    guard = _make_guarded_connect(original_connect)
    setattr(sock_cls, _ORIGINAL_FLAG, original_connect)
    setattr(sock_cls, _GUARD_FLAG, guard)
    sock_cls.connect = guard  # type: ignore[method-assign]
    setattr(sock_cls, _INSTALLED_FLAG, True)
    return True


def uninstall_runtime_socket_guard() -> bool:
    """Restore the original ``socket.socket.connect`` (test helper).

    Safe to call unconditionally, including when another patcher has taken
    over ``connect`` since the guard was installed. In that case the stashed
    original is stale -- putting it back would swap a dead wrapper into the
    process and silently displace the live patch -- so the bookkeeping is
    dropped and ``connect`` is left exactly as found.

    Returns True iff the guard was actually the callable on the class and has
    been replaced by the original it captured.
    """
    sock_cls = socket.socket
    if not getattr(sock_cls, _INSTALLED_FLAG, False):
        return False
    original = getattr(sock_cls, _ORIGINAL_FLAG, None)
    live = _guard_is_live(sock_cls)
    _clear_guard_state(sock_cls)
    if not live or original is None:
        return False
    sock_cls.connect = original  # type: ignore[method-assign]
    return True


def is_runtime_socket_guard_installed() -> bool:
    """Return True iff the guard is currently patched into ``socket.socket``."""
    return _guard_is_live(socket.socket)


def collect_unmonitored_destinations(allowed_specs: Iterable[str]) -> list[str]:
    """Return the active policy's allow-list filtered to monitored entries.

    Helper used by :func:`bernstein.core.distribution.doctor_airgap` to
    cross-check the ``--allow-network`` spec against what the runtime
    guard would actually permit. Kept here so the doctor module does
    not have to import :mod:`socket_guard` directly.
    """
    return [spec for spec in allowed_specs if spec.strip()]
