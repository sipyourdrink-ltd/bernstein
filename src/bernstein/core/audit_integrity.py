"""Backward-compatibility shim — moved to bernstein.core.security.audit_integrity."""

import importlib as _importlib

from bernstein.core.security.audit_integrity import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.audit_integrity")


def __getattr__(name: str):
    return getattr(_real, name)
