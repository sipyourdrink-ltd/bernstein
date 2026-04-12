"""Backward-compat shim: re-exports from bernstein.core.security.sso_oidc."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.sso_oidc")
