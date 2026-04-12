"""Backward-compatibility shim — moved to bernstein.core.persistence.store_factory."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.persistence.store_factory")
