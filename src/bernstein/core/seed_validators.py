"""Backward-compatibility shim — moved to bernstein.core.config.seed_validators."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.config.seed_validators")
