"""Conformance tests for the portable authority envelope (#5055, slices E1-E2).

The envelope exists so that evidence about *authority* -- who acted, under
which grant, and which policy decision allowed it -- can be checked somewhere
other than the Bernstein install that produced it. Two properties carry that
promise and every test below pins one of them:

1. **Independence.** ``verify_cli/bernstein_verify_envelope`` validates the
   committed golden vector in a subprocess where ``import bernstein`` raises,
   with no network reachable. If the verifier ever grows a ``bernstein.*``
   import, the subprocess dies with ``ImportError`` and the test fails.
2. **Recomputation.** A signature proves who said it; the verifier re-derives
   the grant-chain hashes, the decision input hashes and the coverage
   statement from the envelope's own recorded inputs, so a widened scope or a
   silently-dropped decision fails even when the signature is intact.

The vectors under ``tests/fixtures/authority-envelope-vectors/`` are committed,
not minted at test time -- see the README there.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_CLI_ROOT = REPO_ROOT / "verify_cli"
PACKAGE_DIR = VERIFY_CLI_ROOT / "bernstein_verify_envelope"
SCHEMA_PATH = REPO_ROOT / "schemas" / "authority-envelope-v1.json"
VECTOR_DIR = REPO_ROOT / "tests" / "fixtures" / "authority-envelope-vectors"
VALID_VECTOR = VECTOR_DIR / "valid-authority-envelope.json"
TAMPERED_VECTOR = VECTOR_DIR / "tampered-authority-envelope.json"

# Makes ``bernstein`` unimportable, so the subprocess is a real, hostile-to-us
# environment rather than a promise.
_BLOCKER_PRELUDE = '''
import sys


class _BernsteinBlocker:
    """Meta-path finder that refuses every ``bernstein`` import."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "bernstein" or fullname.startswith("bernstein."):
            raise ImportError(f"blocked import of {fullname} (verifier independence probe)")
        return None


sys.meta_path.insert(0, _BernsteinBlocker())
'''

# Runs the verifier's ``__main__`` under that blocker.
_ISOLATED_RUNNER = (
    _BLOCKER_PRELUDE
    + """
import runpy

sys.argv = ["bernstein-verify-envelope", *sys.argv[1:]]
runpy.run_module("bernstein_verify_envelope", run_name="__main__")
"""
)


@pytest.fixture(scope="module")
def isolated_runner(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path to the runner script that blocks ``bernstein`` then starts the CLI."""
    runner = tmp_path_factory.mktemp("envelope-runner") / "run_isolated.py"
    runner.write_text(_ISOLATED_RUNNER, encoding="utf-8")
    return runner


def _run_verifier(runner: Path, envelope: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the standalone verifier over *envelope* with ``bernstein`` blocked."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(VERIFY_CLI_ROOT)
    return subprocess.run(
        [sys.executable, str(runner), "verify", str(envelope), *extra],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _load_valid() -> dict[str, Any]:
    return json.loads(VALID_VECTOR.read_text(encoding="utf-8"))


def _write(tmp_path: Path, doc: dict[str, Any], name: str = "envelope.json") -> Path:
    out = tmp_path / name
    out.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return out


def _mutate(tmp_path: Path, edit: Callable[[dict[str, Any]], None], name: str) -> Path:
    doc = _load_valid()
    edit(doc)
    return _write(tmp_path, doc, name)


# ---------------------------------------------------------------------------
# 1-3. Independence: no bernstein, no network, golden vector passes
# ---------------------------------------------------------------------------


def test_golden_vector_verifies_in_a_subprocess_without_bernstein(isolated_runner: Path) -> None:
    """The committed vector verifies where ``import bernstein`` raises."""
    proc = _run_verifier(isolated_runner, VALID_VECTOR)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OVERALL: PASS" in proc.stdout
    assert "ImportError" not in proc.stderr
    for section in ("principal", "grants", "decisions", "evidence", "coverage", "signature"):
        assert f"[PASS] {section}" in proc.stdout


def test_the_independence_probe_actually_blocks_bernstein() -> None:
    """The blocker used by test 1 is real: importing bernstein under it fails.

    Without this, test 1 could pass merely because nothing tried to import
    ``bernstein`` in a process where it was importable all along.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(VERIFY_CLI_ROOT)
    blocked = subprocess.run(
        [sys.executable, "-c", _BLOCKER_PRELUDE + "\nimport bernstein\n"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert blocked.returncode != 0
    assert "blocked import of bernstein" in blocked.stderr


def test_verifier_package_imports_neither_bernstein_nor_the_network() -> None:
    """The verifier source names no ``bernstein`` module and no network client."""
    sources = sorted(PACKAGE_DIR.glob("*.py"))
    assert sources, f"no verifier sources under {PACKAGE_DIR}"
    forbidden = ("bernstein.", "httpx", "requests", "urllib", "socket", "aiohttp")
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for token in forbidden:
                assert token not in stripped, f"{path.name}: forbidden import {stripped!r}"


# ---------------------------------------------------------------------------
# 4-5. A one-byte mutation fails, and the failure names the section
# ---------------------------------------------------------------------------


def test_one_byte_mutation_in_decisions_names_the_decisions_section(isolated_runner: Path, tmp_path: Path) -> None:
    """Flipping one character of a decision verdict is reported against ``decisions``."""
    path = _mutate(
        tmp_path,
        lambda doc: doc["decisions"][0].__setitem__("verdict", "deny"),
        "mutated-decisions.json",
    )
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "OVERALL: FAIL" in proc.stdout
    assert "[FAIL] section:decisions" in proc.stdout
    assert "[FAIL] signature" in proc.stdout


def test_one_byte_mutation_in_grants_names_the_grants_section(isolated_runner: Path, tmp_path: Path) -> None:
    """The section named is the one that changed, not a hard-coded first section."""
    path = _mutate(
        tmp_path,
        lambda doc: doc["grants"][-1].__setitem__("not_after", "2999-01-01T00:00:00Z"),
        "mutated-grants.json",
    )
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] section:grants" in proc.stdout
    assert "[FAIL] section:decisions" not in proc.stdout


def test_committed_tampered_vector_is_rejected(isolated_runner: Path) -> None:
    """The committed negative vector stays rejected as the format evolves."""
    proc = _run_verifier(isolated_runner, TAMPERED_VECTOR)
    assert proc.returncode == 1
    assert "OVERALL: FAIL" in proc.stdout


# ---------------------------------------------------------------------------
# 6-7. Coverage is a field, not an omission
# ---------------------------------------------------------------------------


def test_envelope_without_a_coverage_section_is_refused(isolated_runner: Path, tmp_path: Path) -> None:
    """Silence about scope is refused outright, not treated as full coverage."""
    path = _mutate(tmp_path, lambda doc: doc.pop("coverage"), "no-coverage.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] coverage" in proc.stdout
    assert "missing" in proc.stdout


def test_coverage_that_hides_an_uncovered_decision_is_refused(isolated_runner: Path, tmp_path: Path) -> None:
    """A decision carrying no evidence must be named in ``coverage.uncovered``."""

    def _hide(doc: dict[str, Any]) -> None:
        doc["coverage"]["uncovered"] = []

    path = _mutate(tmp_path, _hide, "hidden-gap.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] coverage" in proc.stdout


def test_coverage_names_the_gap_on_the_passing_vector(isolated_runner: Path) -> None:
    """The golden vector is deliberately partial and reports its own gap."""
    proc = _run_verifier(isolated_runner, VALID_VECTOR, "--verbose")
    assert proc.returncode == 0
    assert "uncovered" in proc.stdout


# ---------------------------------------------------------------------------
# 8-10. Recomputation: the verifier re-derives, it does not trust
# ---------------------------------------------------------------------------


def test_grant_chain_that_widens_scope_is_rejected(isolated_runner: Path, tmp_path: Path) -> None:
    """A child grant may only narrow its parent's scope."""

    def _widen(doc: dict[str, Any]) -> None:
        child = doc["grants"][-1]
        child["scope"] = sorted({*child["scope"], "repo.admin"})

    path = _mutate(tmp_path, _widen, "widened.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] grants" in proc.stdout
    assert "repo.admin" in proc.stdout


def test_allow_verdict_outside_the_referenced_grant_scope_is_rejected(isolated_runner: Path, tmp_path: Path) -> None:
    """An ``allow`` must follow from the grant it names, not merely be signed."""

    def _overreach(doc: dict[str, Any]) -> None:
        doc["decisions"][0]["action"] = "repo.delete"

    path = _mutate(tmp_path, _overreach, "overreach.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] decisions" in proc.stdout
    assert "outside the scope" in proc.stdout
    assert "repo.delete" in proc.stdout


def test_decision_inputs_hash_is_recomputed_not_trusted(isolated_runner: Path, tmp_path: Path) -> None:
    """Editing a decision's recorded inputs breaks its recomputed input hash."""

    def _edit_inputs(doc: dict[str, Any]) -> None:
        doc["decisions"][0]["inputs"]["role"] = "admin"

    path = _mutate(tmp_path, _edit_inputs, "edited-inputs.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] decisions" in proc.stdout
    assert "inputs_hash" in proc.stdout


def test_decision_taken_after_its_grant_expired_is_rejected(isolated_runner: Path, tmp_path: Path) -> None:
    """Every decision must fall inside the validity window of the grant it cites."""

    def _late(doc: dict[str, Any]) -> None:
        doc["decisions"][0]["timestamp"] = "2999-01-01T00:00:00Z"

    path = _mutate(tmp_path, _late, "late-decision.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] decisions" in proc.stdout


def test_evidence_referencing_an_unknown_decision_is_rejected(isolated_runner: Path, tmp_path: Path) -> None:
    """Evidence must attach to a decision the envelope actually carries."""

    def _dangle(doc: dict[str, Any]) -> None:
        doc["evidence"][0]["decision"] = "d-does-not-exist"

    path = _mutate(tmp_path, _dangle, "dangling-evidence.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] evidence" in proc.stdout


def test_principal_id_rebound_to_another_key_is_rejected(isolated_runner: Path, tmp_path: Path) -> None:
    """The principal's identifier is bound to its key material by a recomputed hash."""

    def _swap_key(doc: dict[str, Any]) -> None:
        doc["principal"]["key"]["x"] = "A" + doc["principal"]["key"]["x"][1:]

    path = _mutate(tmp_path, _swap_key, "rebound-principal.json")
    proc = _run_verifier(isolated_runner, path)
    assert proc.returncode == 1
    assert "[FAIL] principal" in proc.stdout


# ---------------------------------------------------------------------------
# 11-12. The schema is the contract the vector is checked against
# ---------------------------------------------------------------------------


def test_schema_accepts_the_golden_vector() -> None:
    """The committed vector validates against the committed JSON Schema."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=_load_valid(), schema=schema)


def test_schema_requires_the_coverage_section() -> None:
    """Coverage is required by the schema, not only by the verifier."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "coverage" in schema["required"]
    doc = copy.deepcopy(_load_valid())
    doc.pop("coverage")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=doc, schema=schema)
