"""Harness tests for the auditor conformance suite.

These answer none of the 21 questions. They hold the instrument itself
honest: the fixture is a recording rather than a hand-written file, the
reader cannot leave the exported bundle, the verification subprocess
really has no ``bernstein`` to fall back on and no socket to open, and
the scoreboard target reports out of the whole question set.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import offline, recorder
from tests.conformance.auditor.bundle import BundleBoundaryError, BundleReader
from tests.conformance.auditor.questions import QUESTIONS, TOTAL_QUESTIONS

if TYPE_CHECKING:
    from pathlib import Path

REPO_ROOT = recorder.REPO_ROOT
SCOREBOARD = REPO_ROOT / "scripts" / "auditor_conformance.py"


def test_question_registry_lists_all_twenty_one_questions() -> None:
    """The score's denominator is the question set, not what is implemented."""
    assert TOTAL_QUESTIONS == 21
    assert [q.number for q in QUESTIONS] == list(range(1, 22))
    assert len({q.text for q in QUESTIONS}) == 21


def test_bundle_reader_refuses_a_path_outside_the_bundle(bundle_reader: BundleReader) -> None:
    """A vector cannot reach ``.sdd/`` - or anything else - past the root."""
    for escape in (
        "../trust/operator-public-key.pem",
        "../../../../.sdd/audit.jsonl",
        "nested/../../questions.py",
        "/etc/hosts",
    ):
        with pytest.raises(BundleBoundaryError):
            bundle_reader.read_bytes(escape)


def test_bundle_reader_refuses_a_symlink_that_escapes_the_bundle(tmp_path: Path) -> None:
    """Containment is checked after resolution, so a symlink cannot tunnel out."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "kept.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("not evidence\n", encoding="utf-8")
    (root / "escape.json").symlink_to(outside)

    reader = BundleReader.open(root)
    assert reader.read_bytes("kept.json") == b"{}"
    with pytest.raises(BundleBoundaryError):
        reader.read_bytes("escape.json")


def test_verifier_subprocess_cannot_import_bernstein(auditor_env: offline.AuditorEnvironment) -> None:
    """The isolation is proven where verification happens, not asserted in a comment."""
    leaked = offline.probe_import(auditor_env, "bernstein")
    assert leaked.returncode != 0, f"bernstein was importable: {leaked.stdout}"
    assert "No module named 'bernstein'" in leaked.stderr

    present = offline.probe_import(auditor_env, "bernstein_verify_receipt.verify")
    assert present.returncode == 0, present.stderr


def test_verifier_subprocess_cannot_open_a_network_socket(auditor_env: offline.AuditorEnvironment) -> None:
    """Offline is enforced by an audit hook, not left to the verifier's word."""
    blocked = offline.probe_socket(auditor_env)
    assert blocked.returncode != 0, f"a socket was opened: {blocked.stdout}"
    assert offline.NETWORK_DENIED_MARKER in blocked.stderr


def test_committed_fixture_is_a_recording_of_the_scenario(fixture_root: Path, tmp_path: Path) -> None:
    """Re-running the scenario reproduces the committed run receipt byte for byte.

    The receipt binds the journal head, the spine head and the endpoint
    identities of the recorded scenario under a fixed signing key, and
    excludes every wall-clock field. A hand-edited fixture - or a
    scenario that drifted from the bundle that claims to record it -
    diverges here.
    """
    fresh = recorder.record(tmp_path / "recorded")

    committed = fixture_root / recorder.BUNDLE_DIR_NAME
    assert (fresh.bundle_root / recorder.RUN_RECEIPT_NAME).read_bytes() == (
        committed / recorder.RUN_RECEIPT_NAME
    ).read_bytes()

    committed_index = json.loads((committed / recorder.INDEX_NAME).read_text(encoding="utf-8"))
    fresh_index = json.loads((fresh.bundle_root / recorder.INDEX_NAME).read_text(encoding="utf-8"))
    assert fresh_index["run_id"] == committed_index["run_id"]
    assert fresh_index["scenario"] == committed_index["scenario"]
    assert sorted(fresh_index["artefacts"]) == sorted(committed_index["artefacts"])


def test_scoreboard_target_prints_the_score_out_of_twenty_one(tmp_path: Path) -> None:
    """The score is quotable from the target's output without opening the suite."""
    assert SCOREBOARD.is_file()
    env = offline.inherited_env()
    env["BERNSTEIN_AUDITOR_SCORE_JSON"] = str(tmp_path / "score.json")
    completed = subprocess.run(
        [sys.executable, str(SCOREBOARD), "score"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"1/{TOTAL_QUESTIONS}" in completed.stdout

    report = json.loads((tmp_path / "score.json").read_text(encoding="utf-8"))
    assert report["passed"] == [17]


def test_regenerating_the_fixture_rewrites_the_bundle(tmp_path: Path) -> None:
    """The documented regeneration command produces the bundle from a run."""
    destination = tmp_path / "fixture"
    shutil.copytree(REPO_ROOT / recorder.FIXTURE_RELATIVE_PATH, destination)
    (destination / recorder.BUNDLE_DIR_NAME / recorder.RUN_RECEIPT_NAME).write_text("{}", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCOREBOARD), "regenerate", "--destination", str(destination)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=offline.inherited_env(),
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    rewritten = destination / recorder.BUNDLE_DIR_NAME / recorder.RUN_RECEIPT_NAME
    committed = REPO_ROOT / recorder.FIXTURE_RELATIVE_PATH / recorder.BUNDLE_DIR_NAME / recorder.RUN_RECEIPT_NAME
    assert rewritten.read_bytes() == committed.read_bytes()
