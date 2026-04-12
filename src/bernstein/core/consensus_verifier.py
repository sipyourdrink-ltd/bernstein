"""Backward-compatibility shim — moved to bernstein.core.quality.consensus_verifier."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.consensus_verifier")
