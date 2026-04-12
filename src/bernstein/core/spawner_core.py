"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_core."""

import importlib as _importlib

from bernstein.core.agents.spawner_core import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.spawner_core")


def __getattr__(name: str):
    return getattr(_real, name)
