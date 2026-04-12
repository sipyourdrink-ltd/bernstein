"""Backward-compatibility shim — moved to bernstein.core.security.security_incident_response."""

import importlib as _importlib

from bernstein.core.security.security_incident_response import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.security_incident_response")


def __getattr__(name: str):
    return getattr(_real, name)
