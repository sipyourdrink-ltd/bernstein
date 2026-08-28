"""Schedule trigger source - normalises a supervisor fire into TriggerEvent.

The schedule supervisor invokes ``normalize_schedule_fire`` whenever a
registered schedule is due. The returned TriggerEvent flows through the
existing trigger pipeline so resulting tasks land in the regular
orchestrator loop, the same way the other configured sources in this
package land.
"""

from __future__ import annotations

from typing import Any

from bernstein.core.tasks.models import TriggerEvent


def normalize_schedule_fire(
    *,
    schedule_id: str,
    fire_time: float,
    goal: str = "",
    scenario_id: str = "",
    projection_hash: str = "",
    misfire_policy: str = "skip",
    extra: dict[str, Any] | None = None,
) -> TriggerEvent:
    """Normalise an in-project schedule fire into a TriggerEvent.

    Args:
        schedule_id: Stable schedule identifier registered via
            ``bernstein schedule add``.
        fire_time: Canonical Unix epoch of the fire instant. We accept
            float for the TriggerEvent timestamp but the deterministic
            projection downstream pins this to ``int``.
        goal: Free-form goal text from the schedule.
        scenario_id: Optional named scenario id.
        projection_hash: Hash from the projection function. Echoed into
            the event metadata so downstream task-routing or trace
            inspection can cross-reference the audit chain entry without
            re-running the projection.
        misfire_policy: ``"skip"`` or ``"catch_up"`` (for receipts).
        extra: Optional source-specific extras.

    Returns:
        A TriggerEvent with ``source="schedule"``.
    """
    metadata: dict[str, Any] = {
        "source_type": "schedule",
        "schedule_id": schedule_id,
        "fire_time": fire_time,
        "misfire_policy": misfire_policy,
    }
    if scenario_id:
        metadata["scenario_id"] = scenario_id
    if projection_hash:
        metadata["projection_hash"] = projection_hash
    if extra:
        # Defensive: never let an extras payload clobber the canonical
        # keys we own (schedule_id, fire_time, projection_hash). If a
        # caller passes a colliding key we keep ours and drop theirs.
        for key, value in extra.items():
            if key not in metadata:
                metadata[key] = value

    message = goal or (f"scenario:{scenario_id}" if scenario_id else schedule_id)

    return TriggerEvent(
        source="schedule",
        timestamp=fire_time,
        raw_payload={
            "schedule_id": schedule_id,
            "fire_time": fire_time,
            "goal": goal,
            "scenario_id": scenario_id,
            "projection_hash": projection_hash,
            "misfire_policy": misfire_policy,
        },
        message=message[:500],
        metadata=metadata,
    )
