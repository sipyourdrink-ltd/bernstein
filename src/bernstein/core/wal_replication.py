"""Backward-compatibility shim — moved to bernstein.core.persistence.wal_replication."""

import importlib as _importlib

from bernstein.core.persistence.wal_replication import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.wal_replication")


def __getattr__(name: str):
    return getattr(_real, name)
