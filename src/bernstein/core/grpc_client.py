"""Backward-compatibility shim — moved to bernstein.core.protocols.grpc_client."""

import importlib as _importlib

from bernstein.core.protocols.grpc_client import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.protocols.grpc_client")


def __getattr__(name: str):
    return getattr(_real, name)
