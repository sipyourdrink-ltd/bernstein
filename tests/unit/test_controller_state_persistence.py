"""Tests for bernstein.core.orchestration.controller_state sidecar persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.orchestration.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.orchestration.controller_state import (
    CLAIM_CONFLICT_MAX_AGE_S,
    AdaptiveParallelismState,
    ClaimConflictEntry,
    _sidecar_path,
    load,
    save,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_sidecar(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* (creating parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_ap_state(
    *,
    configured_max: int = 6,
    current_max: int = 5,
    slo_constrained_max: int | None = None,
    last_adjustment_reason: str = "initial",
    low_error_since_epoch: float | None = None,
) -> AdaptiveParallelismState:
    return AdaptiveParallelismState(
        configured_max=configured_max,
        current_max=current_max,
        slo_constrained_max=slo_constrained_max,
        last_adjustment_reason=last_adjustment_reason,
        low_error_since_epoch=low_error_since_epoch,
    )


# ---------------------------------------------------------------------------
# load — happy path
# ---------------------------------------------------------------------------


class TestLoad:
    def test_missing_sidecar_returns_naive_state(self, tmp_path: Path) -> None:
        _ap_state, conflict_state = load(tmp_path)
        assert _ap_state.configured_max == 0
        assert _ap_state.current_max == 0
        assert _ap_state.slo_constrained_max is None
        assert _ap_state.last_adjustment_reason == "initial"
        assert _ap_state.low_error_since_epoch is None
        assert conflict_state == {}

    def test_valid_sidecar_restores_state(self, tmp_path: Path) -> None:
        now = time.time()
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 1,
                "adaptive_parallelism": {
                    "configured_max": 8,
                    "current_max": 6,
                    "slo_constrained_max": 4,
                    "last_adjustment_reason": "error_rate_high (25%)",
                    "low_error_since_epoch": now - 100.0,
                },
                "claim_conflict_state": {
                    "task-A": {"episode_count": 2, "backoff_until_epoch": now + 60.0},
                },
                "saved_at_epoch": now,
            },
        )
        _ap_state, conflict_state = load(tmp_path)
        assert _ap_state.configured_max == 8
        assert _ap_state.current_max == 6
        assert _ap_state.slo_constrained_max == 4
        assert _ap_state.last_adjustment_reason == "error_rate_high (25%)"
        assert _ap_state.low_error_since_epoch == pytest.approx(now - 100.0, abs=1.0)
        assert len(conflict_state) == 1
        assert conflict_state["task-A"].episode_count == 2
        assert conflict_state["task-A"].backoff_until_epoch == pytest.approx(now + 60.0, abs=1.0)

    def test_no_conflict_state_key_treated_as_empty(self, tmp_path: Path) -> None:
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 1,
                "adaptive_parallelism": {
                    "configured_max": 4,
                    "current_max": 4,
                    "slo_constrained_max": None,
                    "last_adjustment_reason": "initial",
                    "low_error_since_epoch": None,
                },
            },
        )
        _ap_state, conflict_state = load(tmp_path)
        assert conflict_state == {}

    def test_null_slo_constrained_max_restored(self, tmp_path: Path) -> None:
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 1,
                "adaptive_parallelism": {
                    "configured_max": 6,
                    "current_max": 6,
                    "slo_constrained_max": None,
                    "last_adjustment_reason": "initial",
                    "low_error_since_epoch": None,
                },
            },
        )
        ap_state, _ = load(tmp_path)
        assert ap_state.slo_constrained_max is None


# ---------------------------------------------------------------------------
# load — error handling (never crash, always return naive)
# ---------------------------------------------------------------------------


class TestLoadErrors:
    def test_corrupt_json_returns_naive(self, tmp_path: Path) -> None:
        _sidecar_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _sidecar_path(tmp_path).write_text("{not valid json", encoding="utf-8")
        _ap_state, conflict_state = load(tmp_path)
        assert _ap_state.configured_max == 0
        assert conflict_state == {}

    def test_missing_version_returns_naive(self, tmp_path: Path) -> None:
        _write_sidecar(_sidecar_path(tmp_path), {"adaptive_parallelism": {}})
        ap_state, _ = load(tmp_path)
        assert ap_state.configured_max == 0

    def test_version_mismatch_returns_naive(self, tmp_path: Path) -> None:
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 99,
                "adaptive_parallelism": {
                    "configured_max": 6,
                    "current_max": 6,
                    "slo_constrained_max": None,
                    "last_adjustment_reason": "initial",
                    "low_error_since_epoch": None,
                },
            },
        )
        ap_state, _ = load(tmp_path)
        assert ap_state.configured_max == 0

    def test_missing_adaptive_parallelism_returns_naive(self, tmp_path: Path) -> None:
        _write_sidecar(_sidecar_path(tmp_path), {"version": 1})
        ap_state, _ = load(tmp_path)
        assert ap_state.configured_max == 0

    def test_malformed_conflict_entry_skipped(self, tmp_path: Path) -> None:
        now = time.time()
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 1,
                "adaptive_parallelism": {
                    "configured_max": 4,
                    "current_max": 4,
                    "slo_constrained_max": None,
                    "last_adjustment_reason": "initial",
                    "low_error_since_epoch": None,
                },
                "claim_conflict_state": {
                    "good-task": {"episode_count": 1, "backoff_until_epoch": now + 60.0},
                    "bad-task": "not-a-dict",
                },
            },
        )
        _, conflict_state = load(tmp_path)
        assert "good-task" in conflict_state
        assert "bad-task" not in conflict_state


# ---------------------------------------------------------------------------
# load — age out expired claim-conflict entries
# ---------------------------------------------------------------------------


class TestAgeOut:
    def test_expired_conflict_entries_are_dropped(self, tmp_path: Path) -> None:
        now = time.time()
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 1,
                "adaptive_parallelism": {
                    "configured_max": 4,
                    "current_max": 4,
                    "slo_constrained_max": None,
                    "last_adjustment_reason": "initial",
                    "low_error_since_epoch": None,
                },
                "claim_conflict_state": {
                    "still-valid": {"episode_count": 1, "backoff_until_epoch": now + 60.0},
                    "expired": {"episode_count": 3, "backoff_until_epoch": now - 1.0},
                },
            },
        )
        _, conflict_state = load(tmp_path)
        assert "still-valid" in conflict_state
        assert "expired" not in conflict_state

    def test_entry_at_exact_max_age_is_dropped(self, tmp_path: Path) -> None:
        now = time.time()
        _write_sidecar(
            _sidecar_path(tmp_path),
            {
                "version": 1,
                "adaptive_parallelism": {
                    "configured_max": 4,
                    "current_max": 4,
                    "slo_constrained_max": None,
                    "last_adjustment_reason": "initial",
                    "low_error_since_epoch": None,
                },
                "claim_conflict_state": {
                    "borderline": {
                        "episode_count": 1,
                        "backoff_until_epoch": now - CLAIM_CONFLICT_MAX_AGE_S,
                    },
                },
            },
        )
        _, conflict_state = load(tmp_path)
        # Entry's backoff_until_epoch <= now → dropped
        assert "borderline" not in conflict_state


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


class TestSave:
    def test_write_valid_sidecar(self, tmp_path: Path) -> None:
        now = time.time()
        ap_state = _make_ap_state(configured_max=6, current_max=5, slo_constrained_max=4)
        conflict = {"task-1": ClaimConflictEntry(episode_count=2, backoff_until_epoch=now + 120.0)}
        save(tmp_path, ap_state, conflict)

        data = json.loads(_sidecar_path(tmp_path).read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["adaptive_parallelism"]["configured_max"] == 6
        assert data["adaptive_parallelism"]["current_max"] == 5
        assert data["adaptive_parallelism"]["slo_constrained_max"] == 4
        assert data["adaptive_parallelism"]["last_adjustment_reason"] == "initial"
        assert data["claim_conflict_state"]["task-1"]["episode_count"] == 2
        assert data["claim_conflict_state"]["task-1"]["backoff_until_epoch"] == pytest.approx(now + 120.0, abs=1.0)

    def test_empty_conflict_state_is_roundtrippable(self, tmp_path: Path) -> None:
        ap_state = _make_ap_state(configured_max=3, current_max=3)
        save(tmp_path, ap_state, {})
        loaded_ap, loaded_conflict = load(tmp_path)
        assert loaded_ap.configured_max == 3
        assert loaded_ap.current_max == 3
        assert loaded_conflict == {}


# ---------------------------------------------------------------------------
# AdaptiveParallelism.to_dict / from_dict
# ---------------------------------------------------------------------------


class TestAdaptiveParallelismRoundTrip:
    def test_roundtrip_preserves_state(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 4
        ap._slo_constrained_max = 3
        ap._last_adjustment_reason = "error_rate_high (25%)"
        ap._low_error_since = time.time() - 200.0

        restored = AdaptiveParallelism.from_dict(ap.to_dict())
        assert restored.configured_max == 6
        assert restored._current_max == 4
        assert restored._slo_constrained_max == 3
        assert restored._last_adjustment_reason == "error_rate_high (25%)"
        assert restored._low_error_since == pytest.approx(ap._low_error_since, abs=1.0)

    def test_from_dict_with_configured_max_override(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 4
        restored = AdaptiveParallelism.from_dict(ap.to_dict(), configured_max=8)
        assert restored.configured_max == 8
        assert restored._current_max == 4  # override only affects configured_max default

    def test_from_dict_with_empty_dict_defaults_to_max_1(self) -> None:
        ap = AdaptiveParallelism.from_dict({})
        assert ap.configured_max == 1
        assert ap._current_max == 1

    def test_empty_low_error_since_restored_as_none(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._low_error_since = time.time() - 50.0
        restored = AdaptiveParallelism.from_dict(ap.to_dict())
        assert restored._low_error_since is not None

        ap2 = AdaptiveParallelism(configured_max=6)
        ap2._low_error_since = None
        restored2 = AdaptiveParallelism.from_dict(ap2.to_dict())
        assert restored2._low_error_since is None


# ---------------------------------------------------------------------------
# Orchestrator wiring — basic integration smoke test
# ---------------------------------------------------------------------------


class TestOrchestratorIntegration:
    def test_sidecar_file_path(self, tmp_path: Path) -> None:
        path = _sidecar_path(tmp_path)
        assert path == tmp_path / ".sdd" / "runtime" / "controllers.json"
        assert path.parent.name == "runtime"

    def test_save_and_load_roundtrip_with_real_time(self, tmp_path: Path) -> None:
        ap_state = AdaptiveParallelismState(
            configured_max=10,
            current_max=7,
            slo_constrained_max=None,
            last_adjustment_reason="cpu_recovered",
            low_error_since_epoch=time.time() - 300.0,
        )
        conflict = {
            "t1": ClaimConflictEntry(episode_count=1, backoff_until_epoch=time.time() + 600.0),
            "t2": ClaimConflictEntry(episode_count=3, backoff_until_epoch=time.time() + 10.0),
        }
        save(tmp_path, ap_state, conflict)
        loaded_ap, loaded_conflict = load(tmp_path)
        assert loaded_ap.configured_max == 10
        assert loaded_ap.current_max == 7
        assert loaded_ap.last_adjustment_reason == "cpu_recovered"
        assert len(loaded_conflict) == 2
        assert loaded_conflict["t1"].episode_count == 1
        assert loaded_conflict["t2"].episode_count == 3


# ---------------------------------------------------------------------------
# AdaptiveParallelism.to_adaptive_parallelism_state / from_adaptive_parallelism_state
#


class TestAdaptiveParallelismStateConversion:
    def test_to_adaptive_parallelism_state(self) -> None:
        from bernstein.core.orchestration.adaptive_parallelism import AdaptiveParallelism

        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 4
        ap._slo_constrained_max = 3
        ap._last_adjustment_reason = "error_rate_high (25%)"
        ap._low_error_since = time.time() - 200.0

        state = ap.to_adaptive_parallelism_state()
        assert state.configured_max == 6
        assert state.current_max == 4
        assert state.slo_constrained_max == 3
        assert state.last_adjustment_reason == "error_rate_high (25%)"
        assert state.low_error_since_epoch == pytest.approx(ap._low_error_since, abs=1.0)

    def test_from_adaptive_parallelism_state(self) -> None:
        from bernstein.core.orchestration.adaptive_parallelism import AdaptiveParallelism

        state = _make_ap_state(
            configured_max=8,
            current_max=5,
            slo_constrained_max=4,
            last_adjustment_reason="cpu_recovered",
            low_error_since_epoch=time.time() - 100.0,
        )

        ap = AdaptiveParallelism.from_adaptive_parallelism_state(state)
        assert ap.configured_max == 8
        assert ap._current_max == 5
        assert ap._slo_constrained_max == 4
        assert ap._last_adjustment_reason == "cpu_recovered"
        assert ap._low_error_since == pytest.approx(state.low_error_since_epoch, abs=1.0)

    def test_from_adaptive_parallelism_state_with_override(self) -> None:
        from bernstein.core.orchestration.adaptive_parallelism import AdaptiveParallelism

        state = _make_ap_state(configured_max=6, current_max=4)
        ap = AdaptiveParallelism.from_adaptive_parallelism_state(state, configured_max=10)
        assert ap.configured_max == 10
        assert ap._current_max == 4  # current_max preserved

    def test_roundtrip_orchestrator_path(self, tmp_path: Path) -> None:
        """Test the full orchestration path: AP.to_state -> sidecar -> AP.from_state."""
        from bernstein.core.orchestration.adaptive_parallelism import AdaptiveParallelism

        ap = AdaptiveParallelism(configured_max=10)
        ap._current_max = 7
        ap._slo_constrained_max = 5
        ap._last_adjustment_reason = "slo_budget"
        ap._low_error_since = time.time() - 500.0

        # Save to sidecar
        state = ap.to_adaptive_parallelism_state()
        conflict: dict[str, ClaimConflictEntry] = {}
        save(tmp_path, state, conflict)

        # Restore from sidecar
        loaded_ap, _loaded_conflict = load(tmp_path)
        restored = AdaptiveParallelism.from_adaptive_parallelism_state(loaded_ap)

        assert restored.configured_max == 10
        assert restored._current_max == 7
        assert restored._slo_constrained_max == 5
        assert restored._last_adjustment_reason == "slo_budget"
        assert restored._low_error_since == pytest.approx(ap._low_error_since, abs=1.0)
