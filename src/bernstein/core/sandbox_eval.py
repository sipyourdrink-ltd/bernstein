"""Backward-compat shim: re-exports from bernstein.core.security.sandbox_eval."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.sandbox_eval")
