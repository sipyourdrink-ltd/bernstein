"""Backward-compatibility shim — moved to bernstein.core.communication.mailbox."""

import importlib as _importlib

from bernstein.core.communication.mailbox import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.mailbox")


def __getattr__(name: str):
    return getattr(_real, name)
