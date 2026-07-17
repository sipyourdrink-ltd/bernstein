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
    by_name: dict[str, list[tuple[int, str, str]]] = {}
    for event in chain.query(event_type=EVENT_FLEET_VAR_SET):
        d = event.details
        name = str(d.get("name", ""))
        by_name.setdefault(name, []).append(
            (
                int(d.get("chain_position", -1)),
                str(d.get("old_value_hash", "")),
                str(d.get("new_value_hash", "")),
            )
        )
    for name, writes in by_name.items():
        writes.sort(key=lambda w: w[0])
        for expected_pos, (pos, old_hash, _new) in enumerate(writes):
            if pos != expected_pos:
                errors.append(f"variable {name!r}: write ordinal {pos} out of sequence (expected {expected_pos})")
            prior_new = writes[expected_pos - 1][2] if expected_pos > 0 else ""
            if old_hash != prior_new:
                errors.append(f"variable {name!r}: write {expected_pos} old_value_hash does not chain to prior write")
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
