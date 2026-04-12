"""Backward-compatibility shim — moved to bernstein.core.protocols.grpc_server."""
from bernstein.core.protocols.grpc_server import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.protocols.grpc_server")
def __getattr__(name: str):
    return getattr(_real, name)
