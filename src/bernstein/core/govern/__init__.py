"""Govern plan models for posture diff artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bernstein.core.govern.inventory_models import Inventory, Surface
from bernstein.core.govern.plan_models import GovernPlan, PlanEntry, PlanEntryKind
from bernstein.core.govern.playbook_models import Playbook, PlaybookClause


def compute_plan(
    *,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
    run_id: str,
    timestamp: int,
) -> GovernPlan:
    """Compute the posture diff between *playbook* and *inventory*.

    The playbook declares the required state; the inventory enumerates the
    observed state. Each entry in the plan classifies one surface-level
    mismatch.

    Playbook schema::

        {
          "forbidden": [{"surface": "...", "clause": "..."}],
          "required": [{"surface": "...", "clause": "...", "declared_value": "..."}],
          "permitted": [{"surface": "...", "clause": "...", "declared_ceiling": "..."}]
        }

    Inventory schema::

        {
          "surfaces": [{"surface": "...", "observed_value": "...", "evidence_ref": "..."}]
        }

    Determinism: all fields are pure functions of the input data.
    """
    entries: list[PlanEntry] = []

    # Build inventory lookup
    inventory_map: dict[str, dict[str, str]] = {}
    for s in inventory.get("surfaces", []):
        inventory_map[s["surface"]] = {
            "observed_value": str(s.get("observed_value", "")),
            "evidence_ref": str(s.get("evidence_ref", "")),
        }

    # FORBIDDEN: surfaces in inventory that the playbook forbids
    forbidden_map: dict[str, str] = {s["surface"]: s["clause"] for s in playbook.get("forbidden", [])}
    for surface, inv_data in inventory_map.items():
        if surface in forbidden_map:
            entries.append(
                PlanEntry(
                    kind=PlanEntryKind.FORBIDDEN,
                    surface=surface,
                    evidence_ref=inv_data["evidence_ref"],
                    playbook_clause=forbidden_map[surface],
                    observed_value=inv_data["observed_value"],
                    declared_value=None,
                    timestamp=timestamp,
                )
            )

    # WIDER_CEILING: permitted surfaces whose observed value exceeds declared ceiling
    permitted_map: dict[str, tuple[str, str]] = {}
    for s in playbook.get("permitted", []):
        if "declared_ceiling" in s:
            permitted_map[s["surface"]] = (str(s["declared_ceiling"]), s["clause"])

    for surface, inv_data in inventory_map.items():
        if surface in permitted_map:
            ceiling_str, clause = permitted_map[surface]
            obs_str = inv_data["observed_value"]
            if obs_str and _compare_values(obs_str, ceiling_str) > 0:
                entries.append(
                    PlanEntry(
                        kind=PlanEntryKind.WIDER_CEILING,
                        surface=surface,
                        evidence_ref=inv_data["evidence_ref"],
                        playbook_clause=clause,
                        observed_value=obs_str,
                        declared_value=ceiling_str,
                        timestamp=timestamp,
                    )
                )

    # ABSENT: surfaces the playbook requires but are missing from inventory
    required_clause: dict[str, str] = {}
    required_declared: dict[str, str] = {}
    for s in playbook.get("required", []):
        surface = s["surface"]
        required_clause[surface] = s["clause"]
        required_declared[surface] = str(s.get("declared_value", ""))

    for surface, clause in required_clause.items():
        if surface not in inventory_map:
            entries.append(
                PlanEntry(
                    kind=PlanEntryKind.ABSENT,
                    surface=surface,
                    evidence_ref="",
                    playbook_clause=clause,
                    observed_value=None,
                    declared_value=required_declared.get(surface, ""),
                    timestamp=timestamp,
                )
            )

    inputs_bytes = json.dumps(
        {"playbook": playbook, "inventory": inventory},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    inputs_hash = "sha256:" + hashlib.sha256(inputs_bytes).hexdigest()

    return GovernPlan(
        run_id=run_id,
        entries=tuple(entries),
        inputs_hash=inputs_hash,
        timestamp=timestamp,
    )


def _compare_values(observed: str, ceiling: str) -> int:
    """Compare *observed* to *ceiling*.

    Returns >0 if observed > ceiling, 0 if equal, <0 otherwise.
    """
    try:
        return int(float(observed) - float(ceiling))
    except (ValueError, TypeError):
        return (observed > ceiling) - (observed < ceiling)  # type: ignore[return-value]


__all__ = [
    "GovernPlan",
    "Inventory",
    "PlanEntry",
    "PlanEntryKind",
    "Playbook",
    "PlaybookClause",
    "Surface",
    "compute_plan",
]
