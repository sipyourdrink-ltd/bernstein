"""Backward-compatibility shim — moved to bernstein.core.communication.bulletin_board."""

import importlib as _importlib

from bernstein.core.communication.bulletin_board import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.bulletin_board")


def __getattr__(name: str):
    return getattr(_real, name)
