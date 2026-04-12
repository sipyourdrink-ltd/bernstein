"""Backward-compatibility shim — moved to bernstein.core.routing.route_decision."""
from bernstein.core.routing.route_decision import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.routing.route_decision")
def __getattr__(name: str):
    return getattr(_real, name)
