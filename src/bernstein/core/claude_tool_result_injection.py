"""Backward-compat shim: re-exports from bernstein.core.security.claude_tool_result_injection."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.claude_tool_result_injection")
