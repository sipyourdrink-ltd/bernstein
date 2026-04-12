"""Backward-compatibility shim — moved to bernstein.core.planning.plan_schema."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.planning.plan_schema")
