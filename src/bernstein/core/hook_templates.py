"""Backward-compatibility shim — moved to bernstein.core.config.hook_templates."""

import importlib as _importlib

from bernstein.core.config.hook_templates import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.hook_templates")


def __getattr__(name: str):
    return getattr(_real, name)
