"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_warm_pool."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.spawner_warm_pool")
