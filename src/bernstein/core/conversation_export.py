"""Backward-compatibility shim — moved to bernstein.core.communication.conversation_export."""

import importlib as _importlib

from bernstein.core.communication.conversation_export import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.conversation_export")


def __getattr__(name: str):
    return getattr(_real, name)
