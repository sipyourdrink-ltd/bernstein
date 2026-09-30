"""Meta-test for the unit-suite DNS guard.

The autouse fixture in ``tests/unit/conftest.py`` monkeypatches
``socket.getaddrinfo`` so that any unit test attempting a real DNS lookup
raises ``socket.gaierror``. This test asserts the guard is wired correctly.
"""

from __future__ import annotations

import socket

import pytest


def test_dns_guard_blocks_getaddrinfo() -> None:
    """A bare ``socket.getaddrinfo`` call raises in unit tests."""
    with pytest.raises(socket.gaierror, match=r"blocked in unit tests"):
        socket.getaddrinfo("example.com", 80)
