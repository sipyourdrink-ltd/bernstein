"""Backward-compatibility shim — moved to bernstein.core.git.idempotent_merge."""

import importlib as _importlib

from bernstein.core.git.idempotent_merge import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.idempotent_merge")


def __getattr__(name: str):
    return getattr(_real, name)
