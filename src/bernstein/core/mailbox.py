"""Backward-compatibility shim — moved to bernstein.core.communication.mailbox."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.communication.mailbox")
