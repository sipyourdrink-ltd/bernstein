"""Backward-compatibility shim — moved to bernstein.core.routing.role_classifier."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.routing.role_classifier")
