"""Backward-compatibility shim — moved to bernstein.core.planning.plan_loader."""

import importlib as _importlib

from bernstein.core.planning.plan_loader import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.plan_loader")


def __getattr__(name: str):
    return getattr(_real, name)
