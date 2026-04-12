"""Backward-compatibility shim — moved to bernstein.core.quality.gate_commands."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.gate_commands")
