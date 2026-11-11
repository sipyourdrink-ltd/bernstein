"""The Trivy filesystem scan must not scan the scanner adapters' own fixtures.

Each scanner adapter under ``tests/fixtures/scanners/<tool>/`` keeps a
``target/`` tree of deliberately vulnerable inputs: lockfiles pinning
known-bad packages, an env file with a planted secret, insecure IaC. The
Trivy filesystem gate in ``static-analysis-extended.yml`` runs on push to
``main`` and fails on any HIGH/CRITICAL finding, so a new fixture target
that is not listed in ``skip-dirs`` turns ``main`` red on a finding that is
the fixture's whole point. The gate step is PR-exempt, so a pull request
cannot catch the omission; this test does.

Invariants exercised here:

1. Every ``tests/fixtures/scanners/*/target`` directory is listed in the
   ``skip-dirs`` of every Trivy filesystem step.
2. Every listed skip directory exists, so the list cannot silently go stale.
3. The SARIF step and the failing-gate step skip the same directories, so
   Code Scanning and the gate report on the same tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "static-analysis-extended.yml"
SCANNER_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "scanners"
TRIVY_ACTION = "aquasecurity/trivy-action"


def _trivy_fs_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["trivy-fs"]["steps"]
    found = [
        step
        for step in steps
        if TRIVY_ACTION in str(step.get("uses", "")) and step.get("with", {}).get("scan-type") == "fs"
    ]
    assert len(found) == 2, "trivy-fs should have a SARIF step and a failing-gate step"
    return found


def _skip_dirs(step: dict[str, Any]) -> set[str]:
    raw = str(step.get("with", {}).get("skip-dirs", ""))
    return {part.strip() for part in raw.split(",") if part.strip()}


def _fixture_targets() -> set[str]:
    return {path.relative_to(REPO_ROOT).as_posix() for path in SCANNER_FIXTURES.glob("*/target") if path.is_dir()}


def test_every_scanner_fixture_target_is_skipped_by_trivy_fs() -> None:
    targets = _fixture_targets()
    assert targets, "expected scanner fixture target trees under tests/fixtures/scanners"
    for step in _trivy_fs_steps():
        missing = targets - _skip_dirs(step)
        assert not missing, f"{step['name']!r} scans deliberate fixture trees: {sorted(missing)}"


def test_trivy_fs_skip_dirs_all_exist() -> None:
    for step in _trivy_fs_steps():
        stale = {d for d in _skip_dirs(step) if not (REPO_ROOT / d).is_dir()}
        assert not stale, f"{step['name']!r} skips directories that do not exist: {sorted(stale)}"


def test_trivy_fs_sarif_and_gate_skip_the_same_dirs() -> None:
    sarif, gate = _trivy_fs_steps()
    assert _skip_dirs(sarif) == _skip_dirs(gate)
