"""Unit tests for the #2302 projection entrypoint and its extensions.

Covers the recurring-goal-as-projection additions layered on the #1798
projection: the :func:`project` entrypoint (AC1 determinism), the
recurrence / trigger folding, byte-identity preservation for the
recurrence-free path, and the trigger input hash binding (AC5).
"""

from __future__ import annotations

import json
import subprocess
import sys

from bernstein.core.orchestration.schedule_projection import (
    project,
    project_schedule_fire,
)


class TestProjectDeterminism:
    def test_identical_inputs_byte_identical(self) -> None:
        """AC1: two project() calls with identical inputs return
        byte-identical graphs and graph hashes.
        """
        a = project("sched_x", 1_700_000_000, None, goal="daily digest", recurrence="0 9 * * *")
        b = project("sched_x", 1_700_000_000, None, goal="daily digest", recurrence="0 9 * * *")
        assert a.canonical_bytes == b.canonical_bytes
        assert a.graph_hash == b.graph_hash

    def test_graph_hash_aliases_projection_hash(self) -> None:
        r = project("sched_x", 100, None, goal="g")
        assert r.graph_hash == r.projection_hash

    def test_last_state_folds_into_graph_hash(self) -> None:
        a = project("sched_x", 100, {"cursor": 1}, goal="g")
        b = project("sched_x", 100, {"cursor": 2}, goal="g")
        assert a.graph_hash != b.graph_hash

    def test_recurrence_token_order_independent(self) -> None:
        a = project("sched_x", 100, None, goal="g", recurrence="RRULE:INTERVAL=2;FREQ=DAILY")
        b = project("sched_x", 100, None, goal="g", recurrence="RRULE:FREQ=DAILY;INTERVAL=2")
        assert a.graph_hash == b.graph_hash

    def test_different_recurrence_differs(self) -> None:
        daily = project("sched_x", 100, None, goal="g", recurrence="FREQ=DAILY")
        hourly = project("sched_x", 100, None, goal="g", recurrence="FREQ=HOURLY")
        assert daily.graph_hash != hourly.graph_hash

    def test_hashseed_independent(self) -> None:
        """The graph hash must not depend on PYTHONHASHSEED."""
        code = (
            "from bernstein.core.orchestration.schedule_projection import project;"
            "print(project('s', 100, {'b': 2, 'a': 1}, goal='g', recurrence='FREQ=DAILY').graph_hash)"
        )
        out1 = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": "0"},
        ).stdout.strip()
        out2 = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": "12345"},
        ).stdout.strip()
        assert out1 == out2


class TestByteIdentityPreserved:
    def test_no_recurrence_no_trigger_matches_legacy(self) -> None:
        """A projection with neither recurrence nor trigger must be
        byte-identical to the pre-#2302 recurrence-free projection.
        """
        legacy = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="Send daily digest",
        )
        # Known-good value computed on origin/main before #2302.
        assert legacy.projection_hash == "b0323f98fd799b2d84441887e8885b2de73df61429d1cded3f245c3153e0ee11"

    def test_project_without_recurrence_matches_legacy(self) -> None:
        legacy = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="Send daily digest",
        )
        via_project = project("sched_alpha", 1_700_000_000, None, goal="Send daily digest")
        assert via_project.graph_hash == legacy.projection_hash


class TestTriggerInputHash:
    def test_trigger_event_binds_input_hash(self) -> None:
        """AC5: a trigger-driven fire records the trigger event input hash
        in the projection.
        """
        result = project("sched_x", 100, None, goal="g", trigger_event=b'{"push": true}')
        assert result.trigger_input_hash.startswith("sha256:")
        decoded = json.loads(result.canonical_bytes.decode())
        assert decoded["trigger_input_hash"] == result.trigger_input_hash

    def test_different_trigger_event_differs(self) -> None:
        a = project("sched_x", 100, None, goal="g", trigger_event=b"event-a")
        b = project("sched_x", 100, None, goal="g", trigger_event=b"event-b")
        assert a.graph_hash != b.graph_hash

    def test_no_trigger_omits_hash(self) -> None:
        result = project("sched_x", 100, None, goal="g")
        assert result.trigger_input_hash == ""
        decoded = json.loads(result.canonical_bytes.decode())
        assert "trigger_input_hash" not in decoded

    def test_same_trigger_bytes_reproduce(self) -> None:
        a = project("sched_x", 100, None, goal="g", trigger_event=b"same-bytes")
        b = project("sched_x", 100, None, goal="g", trigger_event=b"same-bytes")
        assert a.graph_hash == b.graph_hash
        assert a.trigger_input_hash == b.trigger_input_hash
