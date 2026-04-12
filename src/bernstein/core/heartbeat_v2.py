"""Backward-compatibility shim — moved to bernstein.core.agents.heartbeat_v2."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.heartbeat_v2")
