"""Backward-compatibility shim — moved to bernstein.core.quality.ci_log_parser."""

import importlib as _importlib

from bernstein.core.quality.ci_log_parser import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.ci_log_parser")


def __getattr__(name: str):
    return getattr(_real, name)
