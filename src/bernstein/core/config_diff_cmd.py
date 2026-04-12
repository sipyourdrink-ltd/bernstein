"""Backward-compatibility shim — moved to bernstein.core.config.config_diff_cmd."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.config.config_diff_cmd")
