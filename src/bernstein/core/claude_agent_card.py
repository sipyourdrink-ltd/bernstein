"""Backward-compatibility shim — moved to bernstein.core.agents.claude_agent_card."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.claude_agent_card")
