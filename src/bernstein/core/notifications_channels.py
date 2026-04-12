"""Backward-compatibility shim — moved to bernstein.core.communication.notifications_channels."""

import importlib as _importlib

from bernstein.core.communication.notifications_channels import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.notifications_channels")


def __getattr__(name: str):
    return getattr(_real, name)
