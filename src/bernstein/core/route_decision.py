"""Backward-compatibility shim — moved to bernstein.core.routing.route_decision."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.routing.route_decision")
