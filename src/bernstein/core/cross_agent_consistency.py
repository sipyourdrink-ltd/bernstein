"""Backward-compatibility shim — moved to bernstein.core.agents.cross_agent_consistency."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.cross_agent_consistency")
