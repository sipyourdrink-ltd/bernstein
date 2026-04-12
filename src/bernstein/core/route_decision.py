"""Backward-compatibility shim — moved to bernstein.core.routing.route_decision."""

import importlib as _importlib

from bernstein.core.routing.route_decision import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.route_decision")


def __getattr__(name: str):
    return getattr(_real, name)
