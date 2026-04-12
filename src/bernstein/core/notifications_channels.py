"""Backward-compatibility shim — moved to bernstein.core.communication.notifications_channels."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.communication.notifications_channels")
