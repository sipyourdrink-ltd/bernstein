"""Backward-compatibility shim — moved to bernstein.core.security.external_policy_hook."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.external_policy_hook")
