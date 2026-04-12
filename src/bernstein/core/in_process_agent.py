"""Backward-compatibility shim — moved to bernstein.core.agents.in_process_agent."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.in_process_agent")
