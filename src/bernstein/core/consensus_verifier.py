"""Backward-compatibility shim — moved to bernstein.core.quality.consensus_verifier."""

import importlib as _importlib

from bernstein.core.quality.consensus_verifier import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.consensus_verifier")


def __getattr__(name: str):
    return getattr(_real, name)
