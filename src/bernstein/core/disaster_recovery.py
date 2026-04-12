"""Backward-compatibility shim — moved to bernstein.core.persistence.disaster_recovery."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.persistence.disaster_recovery")
