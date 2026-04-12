"""Backward-compatibility shim — moved to bernstein.core.config.hook_dry_run."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.config.hook_dry_run")
