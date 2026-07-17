"""Semantic verification of fleet config-plane chain events (#2550).

The HMAC chain already makes any mutated, deleted, or reordered fleet event
fail ``bernstein audit verify``. This module adds a *semantic* pillar on top:
it reconstructs the config-plane state from the chain alone and checks the
invariants the events are supposed to encode, so a record that is
individually well-formed but inconsistent with its family is still caught.

Invariants checked:

* **Variable write lineage.** Per name, the ``fleet.var_set`` write ordinals
  are contiguous from 0, the first write's ``old_value_hash`` is empty, and
  each subsequent write's ``old_value_hash`` equals the prior write's
  ``new_value_hash``. A dropped or spliced write breaks the hash lineage.

* **Connection reference integrity.** Every ``fleet.conn_rotate`` and
  ``fleet.conn_resolve`` names a document that was previously created on the
  chain. A resolve receipt for a document that never existed is a break.

When no fleet events are present the check is a silent pass, matching the
other audit-verify pillars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit_chain import (
    EVENT_FLEET_CONN_CREATE,
    EVENT_FLEET_CONN_RESOLVE,
    EVENT_FLEET_CONN_ROTATE,
    EVENT_FLEET_VAR_SET,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = ["verify_fleet_config_events"]


def _verify_variable_lineage(chain: AuditChainStore) -> list[str]:
    errors: list[str] = []
    # Verify in audit-chain append order, per name. The events are consumed in
    # the order they were recorded (never re-sorted), so a reordered history
    # is a divergence rather than something a normalizing sort hides. A
    # malformed record (e.g. a non-integer chain_position) is reported, not
    # allowed to crash verification.
    expected_next: dict[str, int] = {}
    prior_new_hash: dict[str, str] = {}
    for event in chain.query(event_type=EVENT_FLEET_VAR_SET):
        d = event.details
        name = str(d.get("name", ""))
        raw_position = d.get("chain_position")
        if not isinstance(raw_position, int) or isinstance(raw_position, bool):
            errors.append(f"variable {name!r}: malformed chain_position {raw_position!r}")
            continue
        old_hash = str(d.get("old_value_hash", ""))
        new_hash = str(d.get("new_value_hash", ""))
        want = expected_next.get(name, 0)
        if raw_position != want:
            errors.append(f"variable {name!r}: write ordinal {raw_position} out of sequence (expected {want})")
        if old_hash != prior_new_hash.get(name, ""):
            errors.append(f"variable {name!r}: write {raw_position} old_value_hash does not chain to prior write")
        expected_next[name] = raw_position + 1
        prior_new_hash[name] = new_hash
    return errors


def _verify_connection_references(chain: AuditChainStore) -> list[str]:
    errors: list[str] = []
    created = {str(event.details.get("name", "")) for event in chain.query(event_type=EVENT_FLEET_CONN_CREATE)}
    for event_type, label in (
        (EVENT_FLEET_CONN_ROTATE, "rotate"),
        (EVENT_FLEET_CONN_RESOLVE, "resolve"),
    ):
        for event in chain.query(event_type=event_type):
            name = str(event.details.get("name", ""))
            if name not in created:
                errors.append(f"connection {label} references unknown document {name!r}")
    return errors


def verify_fleet_config_events(chain: AuditChainStore) -> tuple[bool, list[str]]:
    """Return ``(ok, errors)`` for the fleet config-plane semantic invariants.

    ``ok`` is ``True`` when every invariant holds (including the empty case).
    """
    errors = _verify_variable_lineage(chain) + _verify_connection_references(chain)
    return (not errors), errors
