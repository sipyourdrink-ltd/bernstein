"""Backward-compatibility shim — moved to bernstein.core.cost.claude_cost_tracking."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.cost.claude_cost_tracking")
