"""Tunnel driver registrations.

Importing this module installs the four shipped drivers onto the
supplied :class:`~bernstein.core.tunnels.registry.TunnelRegistry`.

The registration is kept pluggy-shaped (a single
:func:`register_default_drivers` entry point) so that external packages
can add their own drivers the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.tunnels.drivers.bore import BoreDriver
from bernstein.core.tunnels.drivers.cloudflared import CloudflaredDriver
from bernstein.core.tunnels.drivers.ngrok import NgrokDriver
from bernstein.core.tunnels.drivers.tailscale import TailscaleDriver

if TYPE_CHECKING:
    from bernstein.core.tunnels.protocol import TunnelProvider
    from bernstein.core.tunnels.registry import TunnelRegistry

__all__ = [
    "BoreDriver",
    "CloudflaredDriver",
    "NgrokDriver",
    "TailscaleDriver",
    "default_drivers",
    "register_default_drivers",
]


def default_drivers() -> list[TunnelProvider]:
    """Return freshly-instantiated copies of the shipped drivers.

    Returns:
        One instance each of cloudflared, ngrok, bore, and tailscale.
    """
    return [CloudflaredDriver(), NgrokDriver(), BoreDriver(), TailscaleDriver()]


def register_default_drivers(registry: TunnelRegistry) -> None:
    """Register every shipped driver onto ``registry``.

    Args:
        registry: Target registry.
    """
    for driver in default_drivers():
        registry.register(driver)
