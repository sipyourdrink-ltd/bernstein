"""Backward-compatibility shim — moved to bernstein.core.server.hooks_receiver."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.server.hooks_receiver")
