"""Standalone authority-envelope verifier for Bernstein.

Validates a portable authority envelope -- principal, grant chain, policy
decisions, evidence hashes, coverage statement and signature -- with only
``cryptography`` and ``click`` as dependencies. Provides the CLI entry point
``bernstein-verify-envelope``.

This package MUST NOT import from bernstein.* at runtime, and MUST NOT open a
network connection.
"""

from __future__ import annotations

__version__ = "1.0.0"
