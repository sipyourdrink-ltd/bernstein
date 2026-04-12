"""Backward-compatibility shim — moved to bernstein.core.security.compliance_policies."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.compliance_policies")
