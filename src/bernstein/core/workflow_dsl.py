"""Backward-compatibility shim — moved to bernstein.core.planning.workflow_dsl."""

import importlib as _importlib

from bernstein.core.planning.workflow_dsl import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.workflow_dsl")


def __getattr__(name: str):
    return getattr(_real, name)
