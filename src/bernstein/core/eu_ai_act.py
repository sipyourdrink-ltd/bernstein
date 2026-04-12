"""Backward-compatibility shim — moved to bernstein.core.security.eu_ai_act."""

import importlib as _importlib

from bernstein.core.security.eu_ai_act import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.eu_ai_act")


def __getattr__(name: str):
    return getattr(_real, name)
