"""Backward-compatibility shim — moved to bernstein.core.agents.warm_pool."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.warm_pool")
