"""Backward-compatibility shim — moved to bernstein.core.plugins_core.skill_md."""

import importlib as _importlib

from bernstein.core.plugins_core.skill_md import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.plugins_core.skill_md")


def __getattr__(name: str):
    return getattr(_real, name)
