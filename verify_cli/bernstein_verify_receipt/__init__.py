"""Standalone audit-receipt verifier package for Bernstein.

Re-implements the verification logic from tools/verify_audit_receipt.py as a
separately installable wheel with only cryptography + cbor2 + click as
dependencies. Provides the CLI entry point ``bernstein-verify-receipt``.

This package MUST NOT import from bernstein.* at runtime.
"""

from __future__ import annotations

__version__ = "1.0.0"
