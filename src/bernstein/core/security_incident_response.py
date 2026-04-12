"""Backward-compatibility shim — moved to bernstein.core.security.security_incident_response."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.security_incident_response")
