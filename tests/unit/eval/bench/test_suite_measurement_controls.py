"""Local synthetic coverage for benchmark measurement-control declarations."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.compliance.controls import Control, ControlRegistry
from bernstein.eval.bench.suite import BenchSuite, BenchTask, validate_measurement_control_ids

if TYPE_CHECKING:
    from pathlib import Path


KNOWN_IDS = ("CTL-GOV-02", "CTL-GOV-01")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture()
def legacy_manifest() -> dict[str, Any]:
    """Build the pre-declaration shape/hash without using BenchSuite serialization."""
    task = {
        "id": "synthetic-task",
        "description": "A local synthetic task",
        "steps": ["observe"],
        "assertions": [],
        "category": "synthetic",
    }
    task_hash = _digest(task)
    return {
        "version": "synthetic-v1",
        "suite_hash": _digest({"version": "synthetic-v1", "task_hashes": [task_hash]}),
        "tasks": [{**task, "task_hash": task_hash}],
    }


@pytest.fixture()
def synthetic_suite(legacy_manifest: dict[str, Any]) -> BenchSuite:
    task = legacy_manifest["tasks"][0]
    return BenchSuite(
        version=legacy_manifest["version"],
        tasks=[
            BenchTask(
                id=task["id"],
                description=task["description"],
                steps=tuple(task["steps"]),
                assertions=tuple(task["assertions"]),
                category=task["category"],
            )
        ],
    )


def test_known_declaration_round_trips(synthetic_suite: BenchSuite, tmp_path: Path) -> None:
    assert validate_measurement_control_ids(list(KNOWN_IDS)) == KNOWN_IDS
    suite = BenchSuite(
        version=synthetic_suite.version,
        tasks=synthetic_suite.tasks,
        measurement_control_ids=KNOWN_IDS,
    )
    assert suite.suite_hash == _digest(
        {
            "version": suite.version,
            "task_hashes": [task.content_hash() for task in suite.tasks],
            "measurement_control_ids": list(KNOWN_IDS),
        }
    )
    assert suite.suite_hash != synthetic_suite.suite_hash
    path = tmp_path / "suite.json"
    suite.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["measurement_control_ids"] == list(KNOWN_IDS)
    loaded = BenchSuite.load(path)
    assert loaded.measurement_control_ids == KNOWN_IDS
    assert loaded.to_dict() == suite.to_dict()


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ([], "measurement_control_ids must not be empty"),
        (
            ["CTL-GOV-02", "CTL-GOV-01", "CTL-GOV-02", "CTL-GOV-01"],
            "measurement_control_ids contains duplicate IDs: CTL-GOV-01, CTL-GOV-02",
        ),
        (
            ["CTL-UNKNOWN-Z", "CTL-UNKNOWN-A"],
            "measurement_control_ids contains unknown IDs: CTL-UNKNOWN-A, CTL-UNKNOWN-Z",
        ),
        (None, "measurement_control_ids must be a non-empty sequence of control IDs"),
        ("CTL-GOV-01", "measurement_control_ids must be a non-empty sequence of control IDs"),
        (["CTL-GOV-01", {}], "measurement_control_ids must contain only strings"),
    ],
)
def test_invalid_declarations_have_deterministic_diagnostics(
    declaration: Any, message: str, legacy_manifest: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(ValueError) as error:
        validate_measurement_control_ids(declaration)
    assert str(error.value) == message

    # Validation must precede the integrity check, even on a legacy manifest.
    legacy_manifest["measurement_control_ids"] = declaration
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        BenchSuite.load(path)
    assert str(error.value) == message


def test_no_declaration_preserves_shape_and_hash(
    synthetic_suite: BenchSuite, legacy_manifest: dict[str, Any], tmp_path: Path
) -> None:
    assert synthetic_suite.measurement_control_ids is None
    assert synthetic_suite.to_dict() == legacy_manifest
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    loaded = BenchSuite.load(path)
    assert loaded.measurement_control_ids is None
    assert loaded.suite_hash == legacy_manifest["suite_hash"]
    assert loaded.to_dict() == legacy_manifest
    loaded.save(path)
    assert json.loads(path.read_text(encoding="utf-8")) == legacy_manifest


@pytest.mark.parametrize("replacement", [None, ["CTL-GOV-01"], list(reversed(KNOWN_IDS))])
def test_declaration_is_bound_to_suite_hash(
    replacement: list[str] | None, synthetic_suite: BenchSuite, tmp_path: Path
) -> None:
    suite = BenchSuite(
        version=synthetic_suite.version,
        tasks=synthetic_suite.tasks,
        measurement_control_ids=KNOWN_IDS,
    )
    manifest = suite.to_dict()
    if replacement is None:
        del manifest["measurement_control_ids"]
    else:
        manifest["measurement_control_ids"] = replacement
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Suite hash mismatch"):
        BenchSuite.load(path)


def test_validator_reads_an_isolated_registry() -> None:
    registry = ControlRegistry(
        controls=[
            Control(
                control_id="CTL-SYNTHETIC-01",
                title="Synthetic control",
                description="Only for this test",
                evidence_kinds=("synthetic",),
            )
        ]
    )
    assert validate_measurement_control_ids(["CTL-SYNTHETIC-01"], registry=registry) == ("CTL-SYNTHETIC-01",)
    with pytest.raises(ValueError, match="measurement_control_ids contains unknown IDs: CTL-GOV-01"):
        validate_measurement_control_ids(["CTL-GOV-01"], registry=registry)
