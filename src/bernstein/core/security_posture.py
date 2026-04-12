"""Backward-compatibility shim — moved to bernstein.core.security.security_posture."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.security_posture")
