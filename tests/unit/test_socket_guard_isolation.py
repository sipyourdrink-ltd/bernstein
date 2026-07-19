"""The runtime socket guard must never write a stale ``connect`` onto the class.

``install_runtime_socket_guard`` stashes whatever ``socket.socket.connect`` was
bound to at install time and parks the stash on the class itself, so a later
``uninstall_runtime_socket_guard`` can put it back. That contract holds only
while *our* guard is still the callable on the class.

Bernstein is not the only thing that patches ``socket.socket.connect``. The unit
suite wraps every test in a hermetic-network guard; an operator shell may have a
tracing or sandbox shim in place. Those patchers restore their own predecessor on
scope exit. If the airgap guard was installed while such a shim was live and was
not uninstalled before the shim went away, the class-level stash now points at a
callable that is no longer anybody's ``connect``. Restoring it -- or trusting the
"installed" flag and skipping a real install -- silently swaps a dead wrapper into
the process, and every subsequent connect in that process runs through it.

These tests pin the identity check that makes install/uninstall safe under that
interleaving.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from bernstein.core.security.network_policy import (
    ENV_NETWORK_POLICY,
    ENV_PROFILE_MODE,
    PROFILE_AIRGAP,
)
from bernstein.core.security.socket_guard import (
    install_runtime_socket_guard,
    is_runtime_socket_guard_installed,
    uninstall_runtime_socket_guard,
)

_GUARD_ATTRS = (
    "_bernstein_socket_guard_installed",
    "_bernstein_socket_guard_original_connect",
    "_bernstein_socket_guard_active_connect",
)

_MISSING = object()


@pytest.fixture(autouse=True)
def _restore_socket_state() -> Iterator[None]:
    """Snapshot every piece of guard state and put it back verbatim.

    These tests deliberately drive the guard into inconsistent states, so the
    teardown cannot go through ``uninstall_runtime_socket_guard`` -- that is the
    function under test. Restore the raw attributes instead.
    """
    saved_connect = socket.socket.connect
    saved_attrs = {name: getattr(socket.socket, name, _MISSING) for name in _GUARD_ATTRS}
    try:
        yield
    finally:
        socket.socket.connect = saved_connect  # type: ignore[method-assign]
        for name, value in saved_attrs.items():
            if value is _MISSING:
                if hasattr(socket.socket, name):
                    delattr(socket.socket, name)
            else:
                setattr(socket.socket, name, value)


def _stashed_original() -> Any:
    """The ``connect`` the guard captured at install time, or ``None``."""
    return getattr(socket.socket, _GUARD_ATTRS[1], None)


def _foreign_patch() -> Any:
    """A stand-in for another patcher's ``connect`` wrapper."""

    def _connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("foreign connect must never be called by these tests")

    return _connect


@pytest.fixture
def _airgap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "none")


@pytest.mark.usefixtures("_airgap_env")
def test_uninstall_after_foreign_patch_withdrawn_leaves_connect_alone() -> None:
    """A withdrawn foreign patch must not come back via our stash.

    Reproduces the suite-wide leak: a test installs the guard while the
    hermetic-network wrapper is live and never uninstalls; the wrapper is then
    withdrawn at teardown. An unrelated later test calls uninstall -- which must
    not resurrect the withdrawn wrapper as the process-wide ``connect``.
    """
    pristine = socket.socket.connect
    foreign = _foreign_patch()

    socket.socket.connect = foreign  # type: ignore[method-assign]
    assert install_runtime_socket_guard() is True
    # The foreign patcher's scope ends and it restores its own predecessor.
    socket.socket.connect = pristine  # type: ignore[method-assign]

    assert uninstall_runtime_socket_guard() is False
    assert socket.socket.connect is pristine
    assert is_runtime_socket_guard_installed() is False


@pytest.mark.usefixtures("_airgap_env")
def test_stale_flags_do_not_block_a_real_install() -> None:
    """Fail-open guard: stale flags must not make install a silent no-op.

    ``install_runtime_socket_guard`` returning True while ``connect`` is
    unpatched would leave an airgap run believing it has an egress boundary it
    does not have.
    """
    pristine = socket.socket.connect
    foreign = _foreign_patch()

    socket.socket.connect = foreign  # type: ignore[method-assign]
    assert install_runtime_socket_guard() is True
    socket.socket.connect = pristine  # type: ignore[method-assign]

    assert install_runtime_socket_guard() is True
    assert socket.socket.connect is not pristine
    assert is_runtime_socket_guard_installed() is True
    # The fresh install must close over the live connect, not the withdrawn one.
    assert _stashed_original() is pristine


@pytest.mark.usefixtures("_airgap_env")
def test_is_installed_reports_false_once_our_guard_is_displaced() -> None:
    """The reported state must track the class, not a leftover flag."""
    pristine = socket.socket.connect

    assert install_runtime_socket_guard() is True
    assert is_runtime_socket_guard_installed() is True

    socket.socket.connect = pristine  # type: ignore[method-assign]
    assert is_runtime_socket_guard_installed() is False


@pytest.mark.usefixtures("_airgap_env")
def test_force_reinstall_after_displacement_does_not_restore_stale_original() -> None:
    """``force=True`` re-derives its original from the live class too."""
    pristine = socket.socket.connect
    foreign = _foreign_patch()

    socket.socket.connect = foreign  # type: ignore[method-assign]
    assert install_runtime_socket_guard(force=True) is True
    socket.socket.connect = pristine  # type: ignore[method-assign]

    assert install_runtime_socket_guard(force=True) is True
    assert _stashed_original() is pristine
    assert uninstall_runtime_socket_guard() is True
    assert socket.socket.connect is pristine


@pytest.mark.usefixtures("_airgap_env")
def test_install_uninstall_round_trip_still_restores() -> None:
    """The ordinary path is unchanged: uninstall puts the original back."""
    pristine = socket.socket.connect

    assert install_runtime_socket_guard() is True
    assert is_runtime_socket_guard_installed() is True
    assert socket.socket.connect is not pristine

    assert uninstall_runtime_socket_guard() is True
    assert socket.socket.connect is pristine
    assert is_runtime_socket_guard_installed() is False


def test_uninstall_is_a_noop_when_never_installed() -> None:
    """No flags, no state change, no lie in the return value."""
    pristine = socket.socket.connect
    assert uninstall_runtime_socket_guard() is False
    assert socket.socket.connect is pristine
