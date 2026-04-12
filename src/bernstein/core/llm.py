"""Backward-compatibility shim — moved to bernstein.core.routing.llm."""

import importlib as _importlib

from bernstein.core.routing.llm import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.llm")


def __getattr__(name: str):
    return getattr(_real, name)
