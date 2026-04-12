"""Backward-compatibility shim — moved to bernstein.core.routing.router_policies."""
import importlib as _importlib

from bernstein.core.routing.router_policies import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.router_policies")
def __getattr__(name: str):
    return getattr(_real, name)
