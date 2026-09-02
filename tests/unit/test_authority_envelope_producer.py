"""Conformance tests for the run-scoped authority-envelope producer (#5055, E3-E4).

The schema and the standalone verifier landed first; nothing wrote an envelope.
This module pins the producer that turns a run's persisted
:class:`~bernstein.core.security.governance.GovernanceDecision` records into an
envelope, and it judges the producer by the verifier that already exists rather
than by a second copy of the format:

1. **The verifier is the oracle.** The load-bearing test writes a real run --
   real lineage spine, real signed role bindings, real decision records on disk
   -- builds an envelope from it, and runs
   ``verify_cli/bernstein_verify_envelope`` over the result in a subprocess
   where ``import bernstein`` raises. A producer that drifts from the format
   fails here, not in a hand-written assertion that drifts with it.
2. **Coverage is computed, not asserted.** The producer states what it left out
   -- records for other subjects, budget records, and carried decisions with no
   lineage anchor -- and the counts come from the records on disk.
3. **The producer refuses rather than launders.** A decision record that claims
   an ``allow`` outside the role's permission set, or one taken after the grant
   expired, is not signed into an envelope: it raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.interop.authority_envelope import (
    AuthorityEnvelopeError,
    build_run_authority_envelope,
)
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
from bernstein.core.security.governance import (
    RoleBindings,
    decide_access,
    decisions_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_CLI_ROOT = REPO_ROOT / "verify_cli"
SCHEMA_PATH = REPO_ROOT / "schemas" / "authority-envelope-v1.json"

HMAC_KEY = b"envelope-producer-test-key"
RUN_ID = "run-envelope-producer"
PRINCIPAL = "urn:bernstein:principal:agent:reviewer-7"
OTHER_SUBJECT = "urn:bernstein:principal:agent:builder-2"
ISSUER = "urn:bernstein:principal:operator:alex"
NOT_AFTER = "2031-01-01T00:00:00Z"
GRANT_ID = "grant-role-operator"
KID = "envelope-signer-1"
IDP_GROUPS = ("eng-operators",)

# A fixed epoch-second timestamp, so the records -- and the envelope built from
# them -- are the same bytes on every run.
BASE_TS = 1767323045

# Mirrors ``governance._BUDGET_ACTION``; asserted by
# ``test_budget_records_are_named_in_the_coverage_statement``.
BUDGET_ACTION = "budget"

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
    """Path to a runner that blocks ``bernstein`` and then starts the verifier."""
    runner = tmp_path_factory.mktemp("producer-runner") / "run_isolated.py"
    runner.write_text(_ISOLATED_RUNNER, encoding="utf-8")
    return runner


def _run_verifier(runner: Path, envelope: Path) -> subprocess.CompletedProcess[str]:
    """Run the standalone verifier over *envelope* with ``bernstein`` blocked."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(VERIFY_CLI_ROOT)
    return subprocess.run(
        [sys.executable, str(runner), "verify", str(envelope), "--verbose"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _bindings() -> RoleBindings:
    """Signed bindings granting the operator role three permissions."""
    return RoleBindings(
        group_to_role={"eng-operators": "operator"},
        role_permissions={"operator": ("deploy", "read", "restart")},
    ).sign(HMAC_KEY)


def _record_access(
    lineage_root: Path,
    *,
    subject: str,
    action: str,
    now: int,
    bindings: RoleBindings,
    groups: tuple[str, ...] = IDP_GROUPS,
) -> None:
    """Write one real, spine-anchored access decision for the run."""
    decide_access(
        run_id=RUN_ID,
        lineage_root=lineage_root,
        hmac_key=HMAC_KEY,
        subject=subject,
        idp_groups=groups,
        action=action,
        bindings=bindings,
        now=now,
    )


def _seeded_run(tmp_path: Path) -> tuple[Path, RoleBindings]:
    """A run with two allowed actions and one denial for the principal."""
    lineage_root = tmp_path / "lineage"
    bindings = _bindings()
    _record_access(lineage_root, subject=PRINCIPAL, action="deploy", now=BASE_TS, bindings=bindings)
    _record_access(lineage_root, subject=PRINCIPAL, action="read", now=BASE_TS + 1, bindings=bindings)
    _record_access(lineage_root, subject=PRINCIPAL, action="rotate-keys", now=BASE_TS + 2, bindings=bindings)
    return lineage_root, bindings


def _build(lineage_root: Path, bindings: RoleBindings, **overrides: Any) -> dict[str, Any]:
    """Build an envelope for the seeded run, with per-test overrides."""
    signing_pem, _ = generate_ed25519_keypair()
    _, principal_public_pem = generate_ed25519_keypair()
    kwargs: dict[str, Any] = {
        "lineage_root": lineage_root,
        "run_id": RUN_ID,
        "principal_id": PRINCIPAL,
        "principal_public_key_pem": principal_public_pem,
        "idp_groups": IDP_GROUPS,
        "bindings": bindings,
        "grant_id": GRANT_ID,
        "grant_issuer": ISSUER,
        "grant_not_after": NOT_AFTER,
        "signing_key_pem": signing_pem,
        "signing_kid": KID,
    }
    kwargs.update(overrides)
    return build_run_authority_envelope(**kwargs)


def _write(tmp_path: Path, envelope: dict[str, Any]) -> Path:
    out = tmp_path / "authority-envelope.json"
    out.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return out


def _decision_record_paths(lineage_root: Path) -> list[Path]:
    return sorted(decisions_dir(lineage_root, RUN_ID).glob("*.json"))


# ---------------------------------------------------------------------------
# 1. The verifier is the oracle
# ---------------------------------------------------------------------------


def test_envelope_built_from_a_run_verifies_without_the_bernstein_package(
    tmp_path: Path, isolated_runner: Path
) -> None:
    """An envelope produced from a real run passes the standalone verifier."""
    lineage_root, bindings = _seeded_run(tmp_path)
    envelope_path = _write(tmp_path, _build(lineage_root, bindings))

    proc = _run_verifier(isolated_runner, envelope_path)

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OVERALL: PASS" in proc.stdout
    assert "ImportError" not in proc.stderr
    for section in ("principal", "grants", "decisions", "evidence", "coverage", "signature"):
        assert f"[PASS] {section}" in proc.stdout or f"[PASS] section:{section}" in proc.stdout


# ---------------------------------------------------------------------------
# 2-6. What the envelope carries, and what it says it left out
# ---------------------------------------------------------------------------


def test_envelope_carries_every_access_decision_for_the_principal(tmp_path: Path) -> None:
    """Three recorded access decisions become three envelope decisions."""
    lineage_root, bindings = _seeded_run(tmp_path)

    envelope = _build(lineage_root, bindings)

    carried = {(d["action"], d["verdict"]) for d in envelope["decisions"]}
    assert carried == {("deploy", "allow"), ("read", "allow"), ("rotate-keys", "deny")}
    assert envelope["principal"]["id"] == PRINCIPAL


def test_decisions_for_other_subjects_are_named_in_the_coverage_statement(
    tmp_path: Path,
) -> None:
    """A record about another subject is excluded, and the count is stated."""
    lineage_root, bindings = _seeded_run(tmp_path)
    _record_access(lineage_root, subject=OTHER_SUBJECT, action="deploy", now=BASE_TS + 3, bindings=bindings)

    envelope = _build(lineage_root, bindings)

    assert len(envelope["decisions"]) == 3
    assert OTHER_SUBJECT not in {d["subject"] for d in envelope["decisions"]}
    assert "1 decision record" in envelope["coverage"]["statement"]
    assert "other subject" in envelope["coverage"]["statement"]


def test_budget_records_are_named_in_the_coverage_statement(tmp_path: Path) -> None:
    """A budget row draws authority from the spend policy, not the role grant."""
    lineage_root, bindings = _seeded_run(tmp_path)
    _record_access(lineage_root, subject=PRINCIPAL, action=BUDGET_ACTION, now=BASE_TS + 4, bindings=bindings)

    envelope = _build(lineage_root, bindings)

    assert BUDGET_ACTION not in {d["action"] for d in envelope["decisions"]}
    assert "1 budget record" in envelope["coverage"]["statement"]


def test_decision_without_a_lineage_anchor_is_declared_uncovered(tmp_path: Path) -> None:
    """A carried decision with no anchor is a gap the envelope must name."""
    lineage_root, bindings = _seeded_run(tmp_path)
    unanchored = _decision_record_paths(lineage_root)[0]
    row = json.loads(unanchored.read_text(encoding="utf-8"))
    row["journal_entry_hash"] = ""
    unanchored.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    envelope = _build(lineage_root, bindings)

    gaps = envelope["coverage"]["uncovered"]
    assert len(gaps) == 1
    assert gaps[0]["action"] == "deploy"
    assert "anchor" in gaps[0]["reason"]
    assert gaps[0]["decision_id"] not in envelope["coverage"]["covered"]
    assert len(envelope["coverage"]["covered"]) == 2


def test_grant_scope_is_the_signed_role_bindings_permission_set(tmp_path: Path) -> None:
    """The single grant link states the authority the run actually resolved."""
    lineage_root, bindings = _seeded_run(tmp_path)

    envelope = _build(lineage_root, bindings)

    (link,) = envelope["grants"]
    assert link["scope"] == ["deploy", "read", "restart"]
    assert link["issuer"] == ISSUER
    assert link["subject"] == PRINCIPAL
    assert link["parent"] is None
    assert envelope["decisions"][0]["policy"]["version"] == bindings.bindings_hash()


def test_decision_inputs_allow_recomputing_the_recorded_governance_hash(
    tmp_path: Path,
) -> None:
    """A reader holding the bindings can re-derive each record's inputs hash."""
    lineage_root, bindings = _seeded_run(tmp_path)

    envelope = _build(lineage_root, bindings)

    for decision in envelope["decisions"]:
        inputs = decision["inputs"]
        preimage = {
            "kind": "access",
            "role": inputs["role"],
            "action": decision["action"],
            "bindings_hash": inputs["bindings_hash"],
        }
        recomputed = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(preimage, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest()
        )
        assert recomputed == inputs["governance_inputs_hash"]


# ---------------------------------------------------------------------------
# 7-8. The producer refuses rather than launders
# ---------------------------------------------------------------------------


def test_allow_outside_the_role_scope_is_refused_by_the_producer(tmp_path: Path) -> None:
    """A tampered record claiming authority it never had is not signed."""
    lineage_root, bindings = _seeded_run(tmp_path)
    tampered = _decision_record_paths(lineage_root)[2]
    row = json.loads(tampered.read_text(encoding="utf-8"))
    assert row["action"] == "rotate-keys"
    row["verdict"] = "allow"
    tampered.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    with pytest.raises(AuthorityEnvelopeError) as excinfo:
        _build(lineage_root, bindings)

    assert "rotate-keys" in str(excinfo.value)
    assert "scope" in str(excinfo.value)


def test_decision_taken_after_the_grant_expiry_is_refused_by_the_producer(
    tmp_path: Path,
) -> None:
    """A decision outside the grant's window would not verify, so it is not emitted."""
    lineage_root, bindings = _seeded_run(tmp_path)

    with pytest.raises(AuthorityEnvelopeError) as excinfo:
        _build(lineage_root, bindings, grant_not_after="2020-01-01T00:00:00Z")

    assert "2020-01-01T00:00:00Z" in str(excinfo.value)


def test_role_resolution_mismatch_is_refused_by_the_producer(tmp_path: Path) -> None:
    """A caller passing groups that resolve to a different role than the record pins is refused."""
    lineage_root = tmp_path / "lineage"
    # Bindings where the same group maps to "viewer", but we'll record as "operator".
    bindings = RoleBindings(
        group_to_role={"eng-operators": "viewer"},
        role_permissions={
            "operator": ("deploy", "read", "restart"),
            "viewer": ("read",),
        },
    ).sign(HMAC_KEY)
    # Record decisions as the operator role using a different group.
    _record_access(
        lineage_root,
        subject=PRINCIPAL,
        action="deploy",
        now=BASE_TS,
        bindings=bindings,
        groups=("eng-operators-admin",),
    )
    # Build the envelope with the viewer group — role differs from what the
    # record pins, so the inputs_hash check must fail.
    with pytest.raises(AuthorityEnvelopeError) as excinfo:
        _build(
            lineage_root,
            bindings,
            idp_groups=("eng-operators",),  # resolves to viewer, not operator
        )
    assert "inputs_hash" in str(excinfo.value) or "role" in str(excinfo.value)


def test_role_resolution_match_allows_the_envelope(tmp_path: Path) -> None:
    """When caller groups resolve to the same role the records pin, the envelope builds."""
    lineage_root, bindings = _seeded_run(tmp_path)
    # Same bindings and groups as the seeded run — must succeed.
    envelope = _build(lineage_root, bindings)
    assert len(envelope["decisions"]) == 3


# ---------------------------------------------------------------------------
# 9-10. Determinism and the committed schema
# ---------------------------------------------------------------------------


def test_two_builds_of_the_same_run_are_byte_identical(tmp_path: Path) -> None:
    """Nothing in the envelope body comes from the clock or from build order."""
    lineage_root, bindings = _seeded_run(tmp_path)
    signing_pem, _ = generate_ed25519_keypair()
    _, principal_pem = generate_ed25519_keypair()

    first = _build(lineage_root, bindings, signing_key_pem=signing_pem, principal_public_key_pem=principal_pem)
    second = _build(lineage_root, bindings, signing_key_pem=signing_pem, principal_public_key_pem=principal_pem)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_produced_envelope_validates_against_the_committed_schema(tmp_path: Path) -> None:
    """The producer's output is the format the schema pins, not a near-miss."""
    import jsonschema

    lineage_root, bindings = _seeded_run(tmp_path)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    jsonschema.validate(instance=_build(lineage_root, bindings), schema=schema)
