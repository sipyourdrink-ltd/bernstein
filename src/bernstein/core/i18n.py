"""Backward-compatibility shim — moved to bernstein.core.config.i18n."""

import importlib as _importlib

from bernstein.core.config.i18n import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.i18n")


def __getattr__(name: str):
    return getattr(_real, name)
