"""Backward-compatibility shim — moved to bernstein.core.protocols.grpc_client."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.protocols.grpc_client")
