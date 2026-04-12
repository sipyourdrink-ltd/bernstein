"""Backward-compat shim: re-exports from bernstein.core.security.pii_output_gate."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.pii_output_gate")
