"""Backward-compatibility shim — moved to bernstein.core.cost.predictive_cost_model."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.cost.predictive_cost_model")
