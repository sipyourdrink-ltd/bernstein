"""Backward-compatibility shim — moved to bernstein.core.agents.agent_recycling."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.agent_recycling")
