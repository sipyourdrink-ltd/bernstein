"""Tests for startup gate checkpoints (T507).

Covers: build, save/load round-trip, staleness detection,
provenance precedence, and failure/corrupt-file handling.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from bernstein.core.session import (
    StartupGateCheckpoint,
    build_startup_gate_checkpoints,
    load_startup_gate_checkpoints,
    save_startup_gate_checkpoints,
)

# ---------------------------------------------------------------------------
# StartupGateCheckpoint dataclass
# ---------------------------------------------------------------------------


class TestStartupGateCheckpoint:
    def test_defaults(self) -> None:
        cp = StartupGateCheckpoint(
            captured_at=1000.0,
            gate_name="lint",
            status="enabled",
        )
        assert cp.cached is False
        assert cp.cache_age_seconds is None
        assert cp.provenance == "default"
        assert cp.config_hash == ""

    def test_is_stale_cache_no_cache(self) -> None:
        cp = StartupGateCheckpoint(
            captured_at=1000.0,
            gate_name="lint",
            status="enabled",
            cached=False,
        )
        assert cp.is_stale_cache() is False

    def test_is_stale_cache_fresh(self) -> None:
        cp = StartupGateCheckpoint(
            captured_at=1000.0,
            gate_name="tests",
            status="cached",
            cached=True,
            cache_age_seconds=600.0,
        )
        assert cp.is_stale_cache(max_age_seconds=3600.0) is False

    def test_is_stale_cache_old(self) -> None:
        cp = StartupGateCheckpoint(
            captured_at=1000.0,
            gate_name="tests",
            status="cached",
            cached=True,
            cache_age_seconds=7200.0,
        )
        assert cp.is_stale_cache(max_age_seconds=3600.0) is True

    def test_is_stale_cache_null_age(self) -> None:
        cp = StartupGateCheckpoint(
            captured_at=1000.0,
            gate_name="lint",
            status="cached",
            cached=True,
            cache_age_seconds=None,
        )
        assert cp.is_stale_cache() is False

    def test_to_dict_round_trip(self) -> None:
        cp = StartupGateCheckpoint(
            captured_at=9999.0,
            gate_name="type_check",
            status="disabled",
            cached=False,
            provenance="seed",
            config_hash="abc123",
        )
        d = cp.to_dict()
        assert d["gate_name"] == "type_check"
        assert d["status"] == "disabled"
        assert d["provenance"] == "seed"
        assert d["config_hash"] == "abc123"

        restored = StartupGateCheckpoint.from_dict(d)
        assert restored.gate_name == cp.gate_name
        assert restored.status == cp.status
        assert restored.provenance == cp.provenance
        assert restored.config_hash == cp.config_hash

    def test_from_dict_missing_captured_at_raises(self) -> None:
        with pytest.raises(KeyError):
            StartupGateCheckpoint.from_dict({"gate_name": "lint", "status": "enabled"})

    def test_from_dict_invalid_captured_at_raises(self) -> None:
        with pytest.raises(ValueError):
            StartupGateCheckpoint.from_dict({"captured_at": "bad", "gate_name": "lint", "status": "enabled"})

    def test_from_dict_unknown_status_falls_back_to_enabled(self) -> None:
        cp = StartupGateCheckpoint.from_dict({"captured_at": 1.0, "gate_name": "lint", "status": "unknown_value"})
        assert cp.status == "enabled"

    def test_from_dict_unknown_provenance_falls_back_to_default(self) -> None:
        cp = StartupGateCheckpoint.from_dict(
            {"captured_at": 1.0, "gate_name": "lint", "status": "enabled", "provenance": "mystery"}
        )
        assert cp.provenance == "default"


# ---------------------------------------------------------------------------
# build_startup_gate_checkpoints — provenance precedence
# ---------------------------------------------------------------------------


class TestBuildStartupGateCheckpoints:
    def test_all_enabled_by_default(self) -> None:
        gates = ["lint", "tests"]
        checkpoints = build_startup_gate_checkpoints(gates)
        statuses = {cp.gate_name: cp.status for cp in checkpoints}
        assert statuses["lint"] == "enabled"
        assert statuses["tests"] == "enabled"

    def test_disabled_gates(self) -> None:
        gates = ["lint", "tests", "type_check"]
        checkpoints = build_startup_gate_checkpoints(
            gates,
            enabled_gates={"lint"},  # only lint is enabled
        )
        statuses = {cp.gate_name: cp.status for cp in checkpoints}
        assert statuses["lint"] == "enabled"
        assert statuses["tests"] == "disabled"
        assert statuses["type_check"] == "disabled"

    def test_cached_gate_overrides_enabled(self) -> None:
        gates = ["lint", "tests"]
        checkpoints = build_startup_gate_checkpoints(
            gates,
            enabled_gates={"lint", "tests"},
            cached_gates={"tests": 120.0},  # tests cached 120s ago
        )
        by_name = {cp.gate_name: cp for cp in checkpoints}
        assert by_name["tests"].status == "cached"
        assert by_name["tests"].cached is True
        assert by_name["tests"].cache_age_seconds == pytest.approx(120.0)
        # lint not cached
        assert by_name["lint"].status == "enabled"
        assert by_name["lint"].cached is False

    def test_provenance_env_wins_over_seed(self) -> None:
        gates = ["lint"]
        checkpoints = build_startup_gate_checkpoints(
            gates,
            seed_gates={"lint"},
            env_gates={"lint"},  # env takes precedence
        )
        assert checkpoints[0].provenance == "env"

    def test_provenance_seed_over_default(self) -> None:
        gates = ["type_check"]
        checkpoints = build_startup_gate_checkpoints(
            gates,
            seed_gates={"type_check"},
        )
        assert checkpoints[0].provenance == "seed"

    def test_provenance_default_when_neither(self) -> None:
        gates = ["security_scan"]
        checkpoints = build_startup_gate_checkpoints(gates)
        assert checkpoints[0].provenance == "default"

    def test_config_hashes_included(self) -> None:
        gates = ["lint"]
        checkpoints = build_startup_gate_checkpoints(
            gates,
            config_hashes={"lint": "deadbeef"},
        )
        assert checkpoints[0].config_hash == "deadbeef"

    def test_empty_gate_list(self) -> None:
        checkpoints = build_startup_gate_checkpoints([])
        assert checkpoints == []

    def test_captured_at_is_recent(self) -> None:
        before = time.time()
        checkpoints = build_startup_gate_checkpoints(["lint"])
        after = time.time()
        assert before <= checkpoints[0].captured_at <= after


# ---------------------------------------------------------------------------
# save/load round-trip
# ---------------------------------------------------------------------------


class TestSaveLoadStartupGateCheckpoints:
    def test_round_trip(self, tmp_path: Path) -> None:
        checkpoints = [
            StartupGateCheckpoint(
                captured_at=1000.0,
                gate_name="lint",
                status="enabled",
                provenance="seed",
            ),
            StartupGateCheckpoint(
                captured_at=1000.0,
                gate_name="tests",
                status="cached",
                cached=True,
                cache_age_seconds=300.0,
                provenance="default",
            ),
        ]
        save_startup_gate_checkpoints(tmp_path, checkpoints)
        loaded = load_startup_gate_checkpoints(tmp_path)
        assert len(loaded) == 2
        assert loaded[0].gate_name == "lint"
        assert loaded[1].gate_name == "tests"
        assert loaded[1].cache_age_seconds == pytest.approx(300.0)

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = load_startup_gate_checkpoints(tmp_path)
        assert result == []

    def test_load_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        gate_file = tmp_path / ".sdd" / "runtime" / "startup_gates.json"
        gate_file.parent.mkdir(parents=True)
        gate_file.write_text("not valid json", encoding="utf-8")
        result = load_startup_gate_checkpoints(tmp_path)
        assert result == []

    def test_load_non_list_json_returns_empty(self, tmp_path: Path) -> None:
        gate_file = tmp_path / ".sdd" / "runtime" / "startup_gates.json"
        gate_file.parent.mkdir(parents=True)
        gate_file.write_text(json.dumps({"unexpected": "dict"}), encoding="utf-8")
        result = load_startup_gate_checkpoints(tmp_path)
        assert result == []

    def test_load_skips_corrupt_entries(self, tmp_path: Path) -> None:
        gate_file = tmp_path / ".sdd" / "runtime" / "startup_gates.json"
        gate_file.parent.mkdir(parents=True)
        # One valid entry, one missing required key
        data = [
            {"captured_at": 1.0, "gate_name": "lint", "status": "enabled"},
            {"gate_name": "tests"},  # missing captured_at
        ]
        gate_file.write_text(json.dumps(data), encoding="utf-8")
        result = load_startup_gate_checkpoints(tmp_path)
        assert len(result) == 1
        assert result[0].gate_name == "lint"

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        # .sdd/runtime doesn't exist yet
        checkpoints = [StartupGateCheckpoint(captured_at=1.0, gate_name="lint", status="enabled")]
        save_startup_gate_checkpoints(tmp_path, checkpoints)
        assert (tmp_path / ".sdd" / "runtime" / "startup_gates.json").exists()

    def test_save_overwrites_previous(self, tmp_path: Path) -> None:
        cp1 = [StartupGateCheckpoint(captured_at=1.0, gate_name="lint", status="enabled")]
        cp2 = [StartupGateCheckpoint(captured_at=2.0, gate_name="tests", status="disabled")]
        save_startup_gate_checkpoints(tmp_path, cp1)
        save_startup_gate_checkpoints(tmp_path, cp2)
        loaded = load_startup_gate_checkpoints(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].gate_name == "tests"
