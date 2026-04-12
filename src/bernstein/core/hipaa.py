"""Backward-compatibility shim — moved to bernstein.core.security.hipaa."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.hipaa")
