"""Backward-compatibility shim — moved to bernstein.core.config.seed_config."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.config.seed_config")
