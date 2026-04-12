"""Backward-compatibility shim — moved to bernstein.core.observability.health_score."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.observability.health_score")
