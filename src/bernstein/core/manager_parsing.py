"""Backward-compatibility shim — moved to bernstein.core.orchestration.manager_parsing."""

import importlib as _importlib

from bernstein.core.orchestration.manager_parsing import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.manager_parsing")


def __getattr__(name: str):
    return getattr(_real, name)
