"""Backward-compatibility shim — moved to bernstein.core.config.seed_parser."""

import importlib as _importlib

from bernstein.core.config.seed_parser import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.seed_parser")


def __getattr__(name: str):
    return getattr(_real, name)
