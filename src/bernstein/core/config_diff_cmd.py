"""Backward-compatibility shim — moved to bernstein.core.config.config_diff_cmd."""

import importlib as _importlib

from bernstein.core.config.config_diff_cmd import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.config_diff_cmd")


def __getattr__(name: str):
    return getattr(_real, name)
