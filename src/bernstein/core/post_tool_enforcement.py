"""Backward-compatibility shim — moved to bernstein.core.security.post_tool_enforcement."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.post_tool_enforcement")
