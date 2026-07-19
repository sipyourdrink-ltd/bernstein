"""Hermetic-network guard for the unit test suite.

Unit tests must be hermetic: they may not open a real outbound connection.
A unit test that talks to a remote host passes only while that host answers
and fails intermittently otherwise (transient 404, DNS hiccup, rate limit),
which turns a green suite red for reasons unrelated to the change under test.
One such test once wedged the merge queue.

This module installs a process-local guard over ``socket.socket.connect`` and
``socket.socket.connect_ex`` that refuses any connection to a non-loopback
address with a clear, actionable :class:`RuntimeError`. Loopback addresses
(``127.0.0.0/8``, ``::1``, ``localhost``) and Unix-domain sockets are allowed
so the many unit tests that spin a local mock server keep working.

The guard is hand-rolled rather than pulled from a third-party plugin so it
adds no dependency to vet, lock, and audit, and so it can integrate with the
suite's existing strict-marker opt-out convention. It is installed per test by
an autouse fixture (see ``tests/unit/conftest.py``); the suite runs each test
file in its own subprocess, so a per-test patch is both sufficient and
deterministic.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

# Loopback hostnames that resolve to a loopback address. Literal IPs are
# checked separately via :func:`ipaddress`.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", ""})

# Real ``socket.socket`` methods, captured once at import time so the guard
# delegates to the genuine implementation for allowed (loopback) connections.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


class NetworkBlockedError(RuntimeError):
    """Raised when a unit test attempts a non-loopback network connection."""


def _inet_parts(address: object) -> tuple[object, ...] | None:
    """Return the address as a typed tuple, or ``None`` for non-inet addresses.

    A ``socket.connect`` address is an ``(host, port[, ...])`` tuple for
    AF_INET / AF_INET6 and a path string for AF_UNIX. Anything that is not such
    a tuple (a Unix-domain path, or any other family) is local IPC and counts
    as allowed, so we return ``None`` for it.
    """
    if isinstance(address, (tuple, list)):
        return tuple(cast("tuple[object, ...]", address))
    return None


def _host_from_address(address: object) -> str | None:
    """Extract the target host from a ``socket.connect`` address argument.

    Returns ``None`` when the address is not an ``(host, port[, ...])`` tuple
    (for example a Unix-domain socket path, which is local IPC and always
    allowed), so the caller treats it as loopback.
    """
    parts = _inet_parts(address)
    if parts:
        host = parts[0]
        if isinstance(host, (bytes, bytearray)):
            return host.decode("ascii", "replace")
        return str(host)
    # AF_UNIX (str/bytes path) or any non-inet family: not a network host.
    return None


def is_loopback_address(address: object) -> bool:
    """Return ``True`` when ``address`` targets loopback or local IPC.

    Non-inet addresses (Unix-domain socket paths) count as local and are
    allowed. For inet addresses, the literal target host is inspected without
    triggering name resolution: a loopback hostname or any address inside a
    loopback range (``127.0.0.0/8``, ``::1``) is allowed.
    """
    host = _host_from_address(address)
    if host is None:
        # Unix-domain socket / non-inet family - always local.
        return True
    if host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    # Strip an IPv6 zone id (e.g. "fe80::1%en0") before parsing.
    bare = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        # A non-loopback hostname (e.g. "example.com"); name resolution would
        # be a network egress in itself, so treat it as blocked.
        return False


def _port_from_address(address: object) -> object:
    parts = _inet_parts(address)
    if parts is not None and len(parts) >= 2:
        return parts[1]
    return "?"


def _blocked_error(address: object) -> NetworkBlockedError:
    host = _host_from_address(address) or str(address)
    port = _port_from_address(address)
    return NetworkBlockedError(
        f"unit tests must not touch the network: blocked connection to "
        f"{host}:{port}. Mock it (see docs/contributing/testing.md), or move a "
        f"genuine integration test to tests/integration/."
    )


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if is_loopback_address(address):
        return _REAL_CONNECT(self, address)
    raise _blocked_error(address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    if is_loopback_address(address):
        return _REAL_CONNECT_EX(self, address)
    raise _blocked_error(address)


@contextmanager
def block_network() -> Iterator[None]:
    """Patch ``socket`` connect methods to block non-loopback egress.

    Reentrant-safe: on exit it restores whatever was installed on entry, not
    the import-time methods. Nesting therefore composes - an inner context
    exiting restores the outer guard rather than re-enabling real egress for
    the remainder of the outer scope. Intended for use by the autouse fixture,
    but usable directly in a ``with`` block for targeted tests.

    The class object is bound once, on entry, and both the patch and the
    restore go through that binding rather than re-resolving ``socket.socket``.
    A purity test proving some code path never opens a socket does so by
    replacing the class in the ``socket`` module namespace
    (``monkeypatch.setattr(socket, "socket", ...)``), and that replacement can
    still be live when this context exits - fixture finalizers do not have to
    unwind in the order that would make re-resolution safe. Re-resolving would
    then write the saved methods onto the stand-in and strand the guard on the
    real class for the rest of the session, breaking every later
    ``allow_network`` test with a failure that points nowhere near the cause.
    """
    sock_cls = socket.socket
    previous_connect = sock_cls.connect
    previous_connect_ex = sock_cls.connect_ex
    sock_cls.connect = _guarded_connect  # type: ignore[method-assign]
    sock_cls.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        sock_cls.connect = previous_connect  # type: ignore[method-assign]
        sock_cls.connect_ex = previous_connect_ex  # type: ignore[method-assign]
