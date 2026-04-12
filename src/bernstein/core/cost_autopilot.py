"""Backward-compatibility shim — moved to bernstein.core.cost.cost_autopilot."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.cost.cost_autopilot")
