"""Backward-compatibility shim — moved to bernstein.core.security.soc2_report."""

import importlib as _importlib

from bernstein.core.security.soc2_report import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.soc2_report")


def __getattr__(name: str):
    return getattr(_real, name)
