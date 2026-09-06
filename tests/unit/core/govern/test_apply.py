"""Contracts for ``govern apply``: bind the outcome to the plan that was reviewed.

Every test drives the real substrate -- a real HMAC audit chain on disk, a real
lineage spine, a real applier that writes real files -- because the properties
under test are about those objects, not about a mock of them.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.run_attestation import kms as _kms

if TYPE_CHECKING:
    from bernstein.core.govern.plan_models import GovernPlan, PlanEntry

from bernstein.core.govern import compute_plan
from bernstein.core.govern.apply import (
    ApplyStatus,
    ChangeOutcome,
    ChangeStatus,
    GovernApplyRefused,
    apply_plan,
    compute_apply_id,
    verify_govern_apply_projection,
)
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.dual_approval import (
    ApprovalChannel,
    ApprovalResponse,
    ApprovalStatus,
    create_approval_request,
    evaluate_approval,
)

HMAC_KEY = b"g" * 32
APPROVER = "operator:alice"
REPO_ROOT = Path(__file__).resolve().parents[4]
VERIFIER_SCRIPT = REPO_ROOT / "tools" / "verify_audit_receipt.py"


# ---------------------------------------------------------------------------
# Fixture posture and environment
# ---------------------------------------------------------------------------


def _playbook(*, with_forbidden: bool = False) -> dict[str, Any]:
    playbook: dict[str, Any] = {
        "permitted": [
            {"surface": "svc:a", "clause": "c-a", "declared_ceiling": "5"},
            {"surface": "svc:b", "clause": "c-b", "declared_ceiling": "5"},
        ],
        "required": [{"surface": "svc:c", "clause": "c-c", "declared_value": "on"}],
        "forbidden": [],
    }
    if with_forbidden:
        playbook["forbidden"] = [{"surface": "svc:x", "clause": "c-x"}]
    return playbook


def _inventory(*, with_forbidden: bool = False) -> dict[str, Any]:
    surfaces = [
        {"surface": "svc:a", "observed_value": "9", "evidence_ref": "e-a"},
        {"surface": "svc:b", "observed_value": "7", "evidence_ref": "e-b"},
    ]
    if with_forbidden:
        surfaces.append({"surface": "svc:x", "observed_value": "present", "evidence_ref": "e-x"})
    return {"surfaces": surfaces}


class _FileApplier:
    """Write each entry's declared value into a real file under *state_dir*.

    ``fail_on`` names a surface whose change raises, so a mid-sequence failure
    is a real failure of a real change rather than a flag flipped in a stub.
    """

    def __init__(self, state_dir: Path, *, fail_on: str | None = None) -> None:
        self.state_dir = state_dir
        self.fail_on = fail_on
        self.attempted: list[str] = []

    def path_for(self, surface: str) -> Path:
        return self.state_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", surface)

    def __call__(self, entry: PlanEntry) -> ChangeOutcome:
        self.attempted.append(entry.surface)
        if entry.surface == self.fail_on:
            raise RuntimeError(f"cannot reach {entry.surface}")
        declared = entry.declared_value or ""
        target = self.path_for(entry.surface)
        if target.exists() and target.read_text(encoding="utf-8") == declared:
            return ChangeOutcome(ChangeStatus.ALREADY_SATISFIED, f"{entry.surface} already at {declared}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(declared, encoding="utf-8")
        return ChangeOutcome(ChangeStatus.APPLIED, f"{entry.surface} set to {declared}")


def _anchored_plan(
    spine_root: Path,
    *,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
) -> tuple[GovernPlan, LineageSpine]:
    """Compute a plan and anchor it in a real lineage spine, as ``plan`` does."""
    plan = compute_plan(playbook=playbook, inventory=inventory, run_id="govern-plan", timestamp=1_700_000_000)
    spine = LineageSpine(spine_root, run_id="govern-plan", hmac_key=HMAC_KEY)
    anchor = spine.record(
        artifact_path="governance-plan.json",
        content=plan.to_canonical_bytes(),
        actor="bernstein.govern",
        step_id=plan.inputs_hash,
        model="none",
        timestamp=1_700_000_000,
    )
    return replace(plan, journal_entry_hash=anchor), spine


def _approval(*, approved: bool = True) -> ApprovalStatus:
    request = create_approval_request("govern apply: remove svc:x", requester=APPROVER)
    if not approved:
        return evaluate_approval(request, [])
    now = datetime.now(tz=UTC).isoformat()
    return evaluate_approval(
        request,
        [
            ApprovalResponse(request.request_id, ApprovalChannel.CLI, "alice", True, now),
            ApprovalResponse(request.request_id, ApprovalChannel.SLACK, "bob", True, now),
        ],
    )


def _apply(
    tmp_path: Path,
    *,
    plan: GovernPlan,
    spine: LineageSpine,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
    applier: _FileApplier,
    removal_approval: ApprovalStatus | None = None,
) -> Any:
    return apply_plan(
        plan=plan,
        playbook=playbook,
        inventory=inventory,
        approver=APPROVER,
        applier=applier,
        audit_dir=tmp_path / "audit",
        key=HMAC_KEY,
        kms_adapter=_kms(tmp_path / "signing"),
        spine=spine,
        removal_approval=removal_approval,
        output_dir=tmp_path / "out",
    )


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    playbook, inventory = _playbook(), _inventory()
    plan, spine = _anchored_plan(tmp_path / "lineage", playbook=playbook, inventory=inventory)
    return {
        "tmp": tmp_path,
        "playbook": playbook,
        "inventory": inventory,
        "plan": plan,
        "spine": spine,
        "applier": _FileApplier(tmp_path / "state"),
    }


def _standard_verify(path: Path) -> tuple[int, str]:
    spec = importlib.util.spec_from_file_location("verify_govern_apply_receipt", VERIFIER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = module.main(["--receipt", str(path)])
    return rc, stdout.getvalue()


# ---------------------------------------------------------------------------
# 1. The declared state, and a receipt naming every change
# ---------------------------------------------------------------------------


def test_apply_produces_the_declared_state_and_a_receipt_naming_every_change(env: dict[str, Any]) -> None:
    applier = env["applier"]
    record = _apply(
        env["tmp"],
        plan=env["plan"],
        spine=env["spine"],
        playbook=env["playbook"],
        inventory=env["inventory"],
        applier=applier,
    )

    assert record.status is ApplyStatus.SUCCESS
    assert [r.surface for r in record.results] == ["svc:a", "svc:b", "svc:c"]
    assert {r.status for r in record.results} == {ChangeStatus.APPLIED}
    # The declared state exists on disk, not merely in the record.
    assert applier.path_for("svc:a").read_text(encoding="utf-8") == "5"
    assert applier.path_for("svc:b").read_text(encoding="utf-8") == "5"
    assert applier.path_for("svc:c").read_text(encoding="utf-8") == "on"

    projection = record.receipt.receipt["govern_apply"]
    assert [c["surface"] for c in projection["changes"]] == ["svc:a", "svc:b", "svc:c"]
    assert projection["approver"] == APPROVER
    assert projection["playbook_digest"]
    assert projection["environment_digest"]
    assert projection["plan_digest"]


# ---------------------------------------------------------------------------
# 2. A review is of a specific world (load-bearing)
# ---------------------------------------------------------------------------


def test_plan_whose_environment_digest_moved_is_refused_before_any_change(env: dict[str, Any]) -> None:
    moved = _inventory()
    moved["surfaces"].append({"surface": "svc:d", "observed_value": "1", "evidence_ref": "e-d"})
    applier = env["applier"]

    with pytest.raises(GovernApplyRefused, match="environment"):
        _apply(
            env["tmp"],
            plan=env["plan"],
            spine=env["spine"],
            playbook=env["playbook"],
            inventory=moved,
            applier=applier,
        )

    assert applier.attempted == []
    assert not (env["tmp"] / "state").exists()


# ---------------------------------------------------------------------------
# 3. A mid-sequence failure stops and the receipt names the last applied change
# ---------------------------------------------------------------------------


def test_mid_sequence_failure_stops_and_the_receipt_names_the_last_applied_change(env: dict[str, Any]) -> None:
    applier = _FileApplier(env["tmp"] / "state", fail_on="svc:b")
    record = _apply(
        env["tmp"],
        plan=env["plan"],
        spine=env["spine"],
        playbook=env["playbook"],
        inventory=env["inventory"],
        applier=applier,
    )

    assert record.status is ApplyStatus.PARTIAL
    assert applier.attempted == ["svc:a", "svc:b"]
    statuses = [(r.surface, r.status) for r in record.results]
    assert statuses == [
        ("svc:a", ChangeStatus.APPLIED),
        ("svc:b", ChangeStatus.FAILED),
        ("svc:c", ChangeStatus.NOT_ATTEMPTED),
    ]
    projection = record.receipt.receipt["govern_apply"]
    assert projection["status"] == "partial"
    assert projection["last_applied_surface"] == "svc:a"
    assert not applier.path_for("svc:c").exists()


def test_terminal_status_is_fail_when_the_first_change_fails(env: dict[str, Any]) -> None:
    applier = _FileApplier(env["tmp"] / "state", fail_on="svc:a")
    record = _apply(
        env["tmp"],
        plan=env["plan"],
        spine=env["spine"],
        playbook=env["playbook"],
        inventory=env["inventory"],
        applier=applier,
    )
    assert record.status is ApplyStatus.FAIL
    assert record.receipt.receipt["govern_apply"]["last_applied_surface"] is None


# ---------------------------------------------------------------------------
# 4. Idempotence
# ---------------------------------------------------------------------------


def test_reapplying_a_complete_plan_changes_nothing_and_still_emits_a_receipt(env: dict[str, Any]) -> None:
    kwargs = {
        "plan": env["plan"],
        "spine": env["spine"],
        "playbook": env["playbook"],
        "inventory": env["inventory"],
    }
    first = _apply(env["tmp"], applier=env["applier"], **kwargs)
    digests_after_first = {p.name: p.read_text(encoding="utf-8") for p in sorted((env["tmp"] / "state").iterdir())}

    second_applier = _FileApplier(env["tmp"] / "state")
    second = _apply(env["tmp"], applier=second_applier, **kwargs)

    assert second.status is ApplyStatus.SUCCESS
    assert {r.status for r in second.results} == {ChangeStatus.ALREADY_SATISFIED}
    assert second.receipt.receipt["govern_apply"]["applied_count"] == 0
    assert {p.name: p.read_text(encoding="utf-8") for p in sorted((env["tmp"] / "state").iterdir())} == (
        digests_after_first
    )
    assert second.receipt.receipt_path is not None
    assert second.apply_id == first.apply_id


# ---------------------------------------------------------------------------
# 5. Offline verification
# ---------------------------------------------------------------------------


def test_apply_receipt_verifies_offline_with_the_standalone_verifier(env: dict[str, Any]) -> None:
    record = _apply(
        env["tmp"],
        plan=env["plan"],
        spine=env["spine"],
        playbook=env["playbook"],
        inventory=env["inventory"],
        applier=env["applier"],
    )
    assert record.receipt.receipt_path is not None
    rc, output = _standard_verify(record.receipt.receipt_path)
    assert rc == 0, output
    assert "OVERALL: PASS" in output

    assert verify_govern_apply_projection(record.receipt.receipt).ok


def test_an_edited_outcome_in_the_receipt_fails_the_projection_check(env: dict[str, Any]) -> None:
    record = _apply(
        env["tmp"],
        plan=env["plan"],
        spine=env["spine"],
        playbook=env["playbook"],
        inventory=env["inventory"],
        applier=env["applier"],
    )
    tampered = json.loads(json.dumps(record.receipt.receipt))
    tampered["govern_apply"]["status"] = "fail"
    result = verify_govern_apply_projection(tampered)
    assert not result.ok
    assert any("status" in err for err in result.errors)


# ---------------------------------------------------------------------------
# 6. The decision record must exist in the journal
# ---------------------------------------------------------------------------


def test_plan_whose_decision_record_is_absent_from_the_journal_is_refused(env: dict[str, Any]) -> None:
    orphan = replace(env["plan"], journal_entry_hash="sha256:" + "0" * 64)
    applier = env["applier"]
    with pytest.raises(GovernApplyRefused, match="journal"):
        _apply(
            env["tmp"],
            plan=orphan,
            spine=env["spine"],
            playbook=env["playbook"],
            inventory=env["inventory"],
            applier=applier,
        )
    assert applier.attempted == []


def test_plan_bytes_that_do_not_match_the_journal_entry_are_refused(env: dict[str, Any]) -> None:
    plan = env["plan"]
    forged = replace(plan, entries=plan.entries[:1])
    applier = env["applier"]
    with pytest.raises(GovernApplyRefused, match="journal"):
        _apply(
            env["tmp"],
            plan=forged,
            spine=env["spine"],
            playbook=env["playbook"],
            inventory=env["inventory"],
            applier=applier,
        )
    assert applier.attempted == []


# ---------------------------------------------------------------------------
# 7. Removal is its own class
# ---------------------------------------------------------------------------


def test_removal_class_entry_is_refused_without_a_satisfied_approval(tmp_path: Path) -> None:
    playbook, inventory = _playbook(with_forbidden=True), _inventory(with_forbidden=True)
    plan, spine = _anchored_plan(tmp_path / "lineage", playbook=playbook, inventory=inventory)
    applier = _FileApplier(tmp_path / "state")

    with pytest.raises(GovernApplyRefused, match="removal"):
        _apply(tmp_path, plan=plan, spine=spine, playbook=playbook, inventory=inventory, applier=applier)
    assert applier.attempted == []

    with pytest.raises(GovernApplyRefused, match="removal"):
        _apply(
            tmp_path,
            plan=plan,
            spine=spine,
            playbook=playbook,
            inventory=inventory,
            applier=applier,
            removal_approval=_approval(approved=False),
        )
    assert applier.attempted == []


def test_removal_class_entry_runs_once_the_approval_is_satisfied(tmp_path: Path) -> None:
    playbook, inventory = _playbook(with_forbidden=True), _inventory(with_forbidden=True)
    plan, spine = _anchored_plan(tmp_path / "lineage", playbook=playbook, inventory=inventory)
    applier = _FileApplier(tmp_path / "state")
    approval = _approval()

    record = _apply(
        tmp_path,
        plan=plan,
        spine=spine,
        playbook=playbook,
        inventory=inventory,
        applier=applier,
        removal_approval=approval,
    )
    assert record.status is ApplyStatus.SUCCESS
    assert "svc:x" in applier.attempted
    assert record.receipt.receipt["govern_apply"]["removal_approval_id"] == approval.request.request_id


# ---------------------------------------------------------------------------
# 8. The whole diff is validated before anything is mutated
# ---------------------------------------------------------------------------


def test_a_surface_named_twice_in_one_plan_is_refused_before_anything_is_mutated(env: dict[str, Any]) -> None:
    plan = env["plan"]
    duplicated = replace(plan, entries=(*plan.entries, plan.entries[0]))
    spine = LineageSpine(env["tmp"] / "lineage", run_id="govern-plan", hmac_key=HMAC_KEY)
    anchor = spine.record(
        artifact_path="governance-plan.json",
        content=replace(duplicated, journal_entry_hash="").to_canonical_bytes(),
        actor="bernstein.govern",
        step_id=duplicated.inputs_hash,
        model="none",
        timestamp=1_700_000_001,
    )
    applier = env["applier"]
    with pytest.raises(GovernApplyRefused, match="more than once"):
        _apply(
            env["tmp"],
            plan=replace(duplicated, journal_entry_hash=anchor),
            spine=spine,
            playbook=env["playbook"],
            inventory=env["inventory"],
            applier=applier,
        )
    assert applier.attempted == []


# ---------------------------------------------------------------------------
# 9. One identifier threads plan, apply record and receipt
# ---------------------------------------------------------------------------


def test_one_apply_id_threads_the_plan_the_record_and_the_receipt(env: dict[str, Any]) -> None:
    record = _apply(
        env["tmp"],
        plan=env["plan"],
        spine=env["spine"],
        playbook=env["playbook"],
        inventory=env["inventory"],
        applier=env["applier"],
    )
    projection = record.receipt.receipt["govern_apply"]
    expected = compute_apply_id(
        plan_digest=record.plan_digest,
        playbook_digest=record.playbook_digest,
        environment_digest=record.environment_digest,
        approver=APPROVER,
    )
    assert record.apply_id == expected == projection["apply_id"]
    assert projection["plan_journal_entry_hash"] == env["plan"].journal_entry_hash
    retained = record.receipt.receipt["events"]
    assert {e["resource_id"] for e in retained} == {record.apply_id}


# ---------------------------------------------------------------------------
# 10. An unread surface can be neither judged compliant nor mutated
# ---------------------------------------------------------------------------


def test_a_plan_entry_for_an_unread_surface_is_refused(tmp_path: Path) -> None:
    playbook = _playbook()
    inventory = _inventory()
    inventory["surfaces"].append({"surface": "svc:c", "observed_value": "", "evidence_ref": "e-c", "unreadable": True})
    plan, spine = _anchored_plan(tmp_path / "lineage", playbook=playbook, inventory=inventory)
    applier = _FileApplier(tmp_path / "state")

    with pytest.raises(GovernApplyRefused, match="could not read"):
        _apply(tmp_path, plan=plan, spine=spine, playbook=playbook, inventory=inventory, applier=applier)
    assert applier.attempted == []
