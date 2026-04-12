"""Backward-compatibility shim — moved to bernstein.core.cost.outcome_pricing."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.cost.outcome_pricing")
