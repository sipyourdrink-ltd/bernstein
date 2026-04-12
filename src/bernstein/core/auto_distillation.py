"""Backward-compatibility shim — moved to bernstein.core.tokens.auto_distillation."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tokens.auto_distillation")
