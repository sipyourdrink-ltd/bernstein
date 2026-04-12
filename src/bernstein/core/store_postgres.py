"""Backward-compatibility shim — moved to bernstein.core.persistence.store_postgres."""

import importlib as _importlib

from bernstein.core.persistence.store_postgres import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.store_postgres")


def __getattr__(name: str):
    return getattr(_real, name)
