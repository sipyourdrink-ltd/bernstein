"""Unit tests for slug derivation and collision suffixing."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from bernstein.core.planning.lifecycle import (
    PlanLifecycle,
    PlanState,
    is_archived_filename,
)
from bernstein.core.planning.run_summary import RunSummary

_SHA_SUFFIX_RE = re.compile(r"-[0-9a-f]{6}\.yaml$")


def _write_active(lifecycle: PlanLifecycle, name: str) -> Path:
    body = yaml.dump({"name": name, "stages": []})
    p = lifecycle.bucket(PlanState.ACTIVE) / f"{name}.yaml"
    p.write_text(body)
    return p


def test_slug_collision_appends_short_hash(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")

    # Archive twice on the same simulated date by patching the clock.
    class FixedClock:
        @staticmethod
        def now(tz: object = None) -> object:
            from datetime import UTC
            from datetime import datetime as real_dt

            return real_dt(2026, 4, 25, tzinfo=UTC)

    lifecycle._clock = FixedClock  # type: ignore[assignment]  # private but tested deliberately

    p1 = _write_active(lifecycle, "demo")
    first = lifecycle.archive_completed(p1, RunSummary(), plan_name="demo")

    p2 = _write_active(lifecycle, "demo")
    second = lifecycle.archive_completed(p2, RunSummary(), plan_name="demo")

    assert first.name == "2026-04-25-demo.yaml"
    assert _SHA_SUFFIX_RE.search(second.name) is not None, second.name
    assert first != second


def test_slug_normalises_special_characters(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    plan_path = _write_active(lifecycle, "Strategic_300!")
    archived = lifecycle.archive_completed(plan_path, RunSummary(), plan_name="Strategic 300!")
    # Slug should be lowercase, dash-separated, alphanumeric only.
    assert "-strategic-300" in archived.name
    assert " " not in archived.name
    assert "!" not in archived.name


def test_archived_filename_pattern_is_recognized() -> None:
    assert is_archived_filename("2026-04-23-strategic-300.yaml")
    assert is_archived_filename("2026-04-23-demo-1a2b3c.yaml")
    assert not is_archived_filename("strategic-300.yaml")
