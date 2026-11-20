"""Check the effective seed values and type errors at the example entry point."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.unit.examples import run_demo

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _use_seed_file_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BERNSTEIN_CONFIG_OVERRIDE", raising=False)
    monkeypatch.setenv("BERNSTEIN_CONFIG_OVERLAY", str(tmp_path / "unused-overlay.yaml"))


def test_bundled_seed_parses_to_the_documented_values(tmp_path: Path) -> None:
    proc = run_demo("quality-gate-seed", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "mutation_testing": True,
        "mutation_threshold": 0.75,
        "large_file_check": True,
        "large_file_threshold": 800,
        "test_expansion": True,
        "intent_verification": {
            "enabled": True,
            "model": "google/gemini-flash-1.5",
            "provider": "openrouter",
            "max_diff_chars": 4000,
            "max_tokens": 128,
            "block_on_no": True,
            "block_on_partial": True,
            "fork_from_context": None,
        },
    }


def test_another_seed_supplies_its_own_typed_values(tmp_path: Path) -> None:
    seed = tmp_path / "custom.yaml"
    seed.write_text("goal: Example\nquality_gates:\n  intent_verification:\n    max_tokens: 73\n", encoding="utf-8")
    proc = run_demo("quality-gate-seed", tmp_path, seed)
    assert proc.returncode == 0, proc.stderr
    values = json.loads(proc.stdout)
    assert values["mutation_testing"] is False
    assert values["intent_verification"]["max_tokens"] == 73


def test_boolean_timeout_is_rejected(tmp_path: Path) -> None:
    seed = tmp_path / "invalid.yaml"
    seed.write_text("goal: Example\nquality_gates:\n  timeout_s: true\n", encoding="utf-8")
    proc = run_demo("quality-gate-seed", tmp_path, seed)
    assert proc.returncode == 2
    assert "quality_gates.timeout_s must be an integer" in proc.stderr
