"""Backward-compatibility shim — moved to bernstein.core.security.compliance_policies."""

import importlib as _importlib

from bernstein.core.security.compliance_policies import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.compliance_policies")


def __getattr__(name: str):
    return getattr(_real, name)
