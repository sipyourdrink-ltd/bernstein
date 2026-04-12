"""Backward-compatibility shim — moved to bernstein.core.agents.spawner_merge."""

import importlib as _importlib

from bernstein.core.agents.spawner_merge import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.spawner_merge")


def __getattr__(name: str):
    return getattr(_real, name)
