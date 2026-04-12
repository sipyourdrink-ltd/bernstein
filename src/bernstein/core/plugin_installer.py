"""Backward-compatibility shim — moved to bernstein.core.plugins_core.plugin_installer."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.plugins_core.plugin_installer")
