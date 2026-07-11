"""Data and ops modality activities on the typed boundary (issue #2311).

The typed activity boundary generalizes past research and browser to the *data*
and *ops* modalities. Unlike a read-only research fetch, a data/ops activity
changes the world, so it carries two extra guarantees the epic calls for:

* a deterministic-plan vs side-effecting split -- the plan is a byte-identical
  projection of the signed inputs, derived *before* any side effect, so a replay
  recomputes the same plan hash from the same inputs; and
* signed input/output artifacts -- every input and output is content-addressed
  *and* Ed25519-signed, so a verifier confirms offline both which exact bytes
  crossed the boundary and that the install produced them.

Both modalities still produce an ``ActivityResult`` the deterministic scheduler
dispatches and journals identically to research/browser: the signed receipt is
the ``artifact`` (its hash anchored as ``artifact_hash``) and the signed
input/output bytes are the content-addressed observations (anchored via
``evidence_set_hash``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.activity import (
    ActivityKind,
    TerminalState,
    dispatch_activity,
)
from bernstein.core.orchestration.activity_modalities import (
    ContentStore,
    DataActivity,
    DataOpsPhaseError,
    DataOpsPlan,
    DataOpsReceipt,
    OpsActivity,
    replay_reattach,
    verify_data_ops_receipt,
    verify_run_activities,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.skills.catalog.signature import generate_signer_keypair


def _keypair() -> tuple[str, str]:
    return generate_signer_keypair()


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


# ---------------------------------------------------------------------------
# deterministic-plan vs side-effecting split
# ---------------------------------------------------------------------------


def test_plan_is_deterministic_projection_of_inputs(tmp_path: Path) -> None:
    priv, pub = _keypair()
    # Two runs with the same signed inputs and the same steps derive the
    # byte-identical plan hash, whatever order the inputs were added in.
    a = DataActivity(store=ContentStore(tmp_path / "cas1"), private_key_pem=priv, public_key_pem=pub)
    a.add_input(ref="rows.csv", content=b"id,name\n1,a\n")
    a.add_input(ref="schema.json", content=b'{"cols":2}')
    plan_a = a.plan(["normalize", "dedupe"])

    b = OpsActivity(store=ContentStore(tmp_path / "cas2"), private_key_pem=priv, public_key_pem=pub)
    b.add_input(ref="schema.json", content=b'{"cols":2}')  # reverse order
    b.add_input(ref="rows.csv", content=b"id,name\n1,a\n")
    plan_b = b.plan(["normalize", "dedupe"])

    assert plan_a.plan_hash == plan_b.plan_hash
    assert plan_a.plan_hash.startswith("sha256:")


def test_plan_hash_changes_when_inputs_change(tmp_path: Path) -> None:
    priv, pub = _keypair()
    a = DataActivity(store=ContentStore(tmp_path / "cas1"), private_key_pem=priv, public_key_pem=pub)
    a.add_input(ref="rows.csv", content=b"alpha")
    plan_a = a.plan(["load"])

    b = DataActivity(store=ContentStore(tmp_path / "cas2"), private_key_pem=priv, public_key_pem=pub)
    b.add_input(ref="rows.csv", content=b"beta")
    plan_b = b.plan(["load"])

    assert plan_a.plan_hash != plan_b.plan_hash


def test_side_effect_before_plan_is_refused(tmp_path: Path) -> None:
    priv, pub = _keypair()
    act = OpsActivity(store=ContentStore(tmp_path / "cas"), private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="target", content=b"host=db")
    # A side-effecting output cannot be recorded before a deterministic plan.
    with pytest.raises(DataOpsPhaseError):
        act.add_output(ref="applied", content=b"done")


def test_input_after_plan_is_refused(tmp_path: Path) -> None:
    priv, pub = _keypair()
    act = DataActivity(store=ContentStore(tmp_path / "cas"), private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="a", content=b"a")
    act.plan(["go"])
    # Inputs are frozen once the plan is derived: no input may change the plan
    # the side effects were computed from.
    with pytest.raises(DataOpsPhaseError):
        act.add_input(ref="b", content=b"b")


def test_finish_before_plan_is_refused(tmp_path: Path) -> None:
    priv, pub = _keypair()
    act = DataActivity(store=ContentStore(tmp_path / "cas"), private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="a", content=b"a")
    with pytest.raises(DataOpsPhaseError):
        act.finish()


# ---------------------------------------------------------------------------
# signed input/output artifacts
# ---------------------------------------------------------------------------


def test_inputs_and_outputs_are_signed(tmp_path: Path) -> None:
    priv, pub = _keypair()
    act = OpsActivity(store=ContentStore(tmp_path / "cas"), private_key_pem=priv, public_key_pem=pub)
    inp = act.add_input(ref="target", content=b"host=db")
    act.plan(["apply-migration"])
    out = act.add_output(ref="result", content=b"rows=3")

    assert inp.role == "input"
    assert out.role == "output"
    assert inp.signature and out.signature
    assert inp.content_hash.startswith("sha256:")


def test_receipt_verifies_signatures_and_plan(tmp_path: Path) -> None:
    priv, pub = _keypair()
    store = ContentStore(tmp_path / "cas")
    act = DataActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="rows.csv", content=b"raw")
    act.plan(["normalize"])
    act.add_output(ref="clean.csv", content=b"clean")
    result = act.finish()

    receipt = DataOpsReceipt.from_dict(result.artifact)
    verdict = verify_data_ops_receipt(receipt, store=store)
    assert verdict.ok
    assert verdict.plan_ok
    assert verdict.signatures_ok
    assert verdict.evidence_reattached


def test_receipt_rejects_forged_signature(tmp_path: Path) -> None:
    priv, pub = _keypair()
    _other_priv, other_pub = _keypair()
    store = ContentStore(tmp_path / "cas")
    act = DataActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="rows.csv", content=b"raw")
    act.plan(["normalize"])
    result = act.finish()

    # Verifying against a different install's public key must fail the signatures.
    tampered = DataOpsReceipt.from_dict({**result.artifact, "signer_public_key_pem": other_pub})
    verdict = verify_data_ops_receipt(tampered, store=store)
    assert not verdict.ok
    assert not verdict.signatures_ok


def test_receipt_rejects_tampered_plan(tmp_path: Path) -> None:
    priv, pub = _keypair()
    store = ContentStore(tmp_path / "cas")
    act = OpsActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="target", content=b"host=db")
    act.plan(["apply"])
    result = act.finish()

    forged = dict(result.artifact)
    forged_plan = dict(forged["plan"])
    forged_plan["steps"] = ["apply", "drop-table"]  # steps not what plan_hash covers
    forged["plan"] = forged_plan
    receipt = DataOpsReceipt.from_dict(forged)
    verdict = verify_data_ops_receipt(receipt, store=store)
    assert not verdict.ok
    assert not verdict.plan_ok


# ---------------------------------------------------------------------------
# journaled identically to research/browser
# ---------------------------------------------------------------------------


def test_data_activity_journals_like_other_modalities(tmp_path: Path) -> None:
    priv, pub = _keypair()
    store = ContentStore(tmp_path / "cas")
    act = DataActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="rows.csv", content=b"raw")
    act.plan(["normalize"])
    act.add_output(ref="clean.csv", content=b"clean")
    result = act.finish()

    assert result.kind is ActivityKind.DATA
    assert result.terminal_state is TerminalState.COMPLETED

    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="data-0", journal=journal)
    rows = load_events(journal.path)
    assert rows[0]["event"] == "activity.result"
    assert rows[0]["kind"] == "data"
    # The signed input/output bytes are content-addressed observations, so a
    # replay reattaches them byte-identically like any other modality.
    reattached = replay_reattach(journal.path, store=store, stage_id="data-0")
    assert b"raw" in reattached
    assert b"clean" in reattached


def test_ops_activity_journals_kind(tmp_path: Path) -> None:
    priv, pub = _keypair()
    store = ContentStore(tmp_path / "cas")
    act = OpsActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="target", content=b"host=db")
    act.plan(["apply"])
    act.add_output(ref="result", content=b"ok")
    result = act.finish()
    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="ops-0", journal=journal)
    rows = load_events(journal.path)
    assert rows[0]["kind"] == "ops"


# ---------------------------------------------------------------------------
# verify_run_activities re-verifies the signed receipt offline
# ---------------------------------------------------------------------------


def test_verify_run_reverifies_signed_receipt(tmp_path: Path) -> None:
    priv, pub = _keypair()
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    act = DataActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="rows.csv", content=b"raw")
    act.plan(["normalize"])
    act.add_output(ref="clean.csv", content=b"clean")
    result = act.finish()

    journal = EventJournal(run_id="run-9", sdd_dir=sdd)
    dispatch_activity(result, stage_id="data-0", journal=journal)

    verified = verify_run_activities(sdd, run_id="run-9", store=store)
    assert verified.ok
    stage = verified.stages[0]
    assert stage.ok
    assert stage.signed_receipt_verified


def test_verify_run_detects_tampered_receipt(tmp_path: Path) -> None:
    priv, pub = _keypair()
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    act = OpsActivity(store=store, private_key_pem=priv, public_key_pem=pub)
    act.add_input(ref="target", content=b"host=db")
    act.plan(["apply"])
    result = act.finish()

    journal = EventJournal(run_id="run-9", sdd_dir=sdd)
    dispatch_activity(result, stage_id="ops-0", journal=journal)

    # Overwrite the stored receipt blob with bytes that no longer match the
    # anchored artifact_hash: verification must refuse the stage.
    store.force_put(result.artifact_hash, b'{"kind":"ops","tampered":true}')
    verified = verify_run_activities(sdd, run_id="run-9", store=store)
    assert not verified.ok
    assert not verified.stages[0].ok


def test_plan_roundtrips_through_dict() -> None:
    plan = DataOpsPlan.derive_from_hashes(input_hashes=["sha256:bb", "sha256:aa"], steps=["s1"])
    restored = DataOpsPlan.from_dict(plan.to_dict())
    assert restored.plan_hash == plan.plan_hash
    # input_hashes are stored sorted+deduplicated for a stable projection.
    assert restored.input_hashes == ("sha256:aa", "sha256:bb")
