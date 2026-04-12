"""Backward-compatibility shim — moved to bernstein.core.protocols.ssh_backend."""

import importlib as _importlib

from bernstein.core.protocols.ssh_backend import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.protocols.ssh_backend")


def __getattr__(name: str):
    return getattr(_real, name)
