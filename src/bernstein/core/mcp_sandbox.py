"""Backward-compatibility shim."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.protocols.mcp_sandbox")
