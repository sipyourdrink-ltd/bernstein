"""Backward-compatibility shim — moved to bernstein.core.observability.error_classifier."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.observability.error_classifier")
