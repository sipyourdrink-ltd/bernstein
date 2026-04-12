"""Backward-compatibility shim — moved to bernstein.core.planning.scenario_library."""

import importlib as _importlib

from bernstein.core.planning.scenario_library import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.scenario_library")


def __getattr__(name: str):
    return getattr(_real, name)
