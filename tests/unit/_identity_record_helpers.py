"""Shared helper for tests that hand-edit persisted agent identity records.

The store ignores any record whose MAC does not verify, so a test that edits a
record to exercise a *field* guard (tenant, token type, scope shape, ...) would
otherwise pass because the MAC failed, not because the guard it names refused
the value.  :func:`seal` re-authenticates the edited record with the per-test
install key, so the guard under test is what decides.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

from bernstein.core.identity.agent_jwt import _record_mac
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.key_derivation import DOMAIN_AGENT_IDENTITY_RECORD, derive_store_key


def seal(payload: dict[str, Any]) -> dict[str, Any]:
    """Return *payload* with a valid ``record_mac`` for the current install key."""
    key = derive_store_key(load_or_create_audit_key(), DOMAIN_AGENT_IDENTITY_RECORD)
    return {**payload, "record_mac": _record_mac(key, payload)}
