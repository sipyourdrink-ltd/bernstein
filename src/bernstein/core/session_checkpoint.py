"""Backward-compatibility shim — moved to bernstein.core.persistence.session_checkpoint."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.persistence.session_checkpoint")
