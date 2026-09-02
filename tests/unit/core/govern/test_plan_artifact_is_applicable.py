"""The plan artifact ``govern plan`` writes must be one ``govern apply`` accepts.

``apply`` refuses a diff whose decision record it cannot find in the journal.
That refusal is only useful if the plan file the operator hands it names the
record it was anchored under, so this test drives the real command against a
real lineage spine and checks the two ends meet.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import governance_group
from bernstein.core.govern.apply import validate_apply
from bernstein.core.govern.plan_models import GovernPlan
from bernstein.core.lineage.spine import LineageSpine, content_hash_of

PLAYBOOK = {
    "permitted": [{"surface": "svc:a", "clause": "c-a", "declared_ceiling": "5"}],
    "required": [],
    "forbidden": [],
}
INVENTORY = {"surfaces": [{"surface": "svc:a", "observed_value": "9", "evidence_ref": "e-a"}]}


@pytest.fixture
def planned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[GovernPlan, LineageSpine]:
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))

    (tmp_path / "playbook.json").write_text(json.dumps(PLAYBOOK), encoding="utf-8")
    (tmp_path / "inventory.json").write_text(json.dumps(INVENTORY), encoding="utf-8")

    result = CliRunner().invoke(
        governance_group,
        [
            "plan",
            "--playbook",
            str(tmp_path / "playbook.json"),
            "--inventory",
            str(tmp_path / "inventory.json"),
            "--workdir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    lineage_root = tmp_path / ".sdd" / "lineage"
    plan = GovernPlan.from_dict(json.loads((lineage_root / "govern-plan" / "plan.json").read_text(encoding="utf-8")))
    spine = LineageSpine(lineage_root, run_id="govern-plan", hmac_key=key_path.read_bytes().strip())
    return plan, spine


def test_written_plan_names_the_journal_entry_that_anchored_its_bytes(
    planned: tuple[GovernPlan, LineageSpine],
) -> None:
    plan, spine = planned
    assert plan.journal_entry_hash
    anchored = [e for e in spine.iter_entries() if e.entry_hash == plan.journal_entry_hash]
    assert len(anchored) == 1
    assert anchored[0].content_hash == content_hash_of(replace(plan, journal_entry_hash="").to_canonical_bytes())


def test_written_plan_passes_the_apply_validation_gate(
    planned: tuple[GovernPlan, LineageSpine],
) -> None:
    plan, spine = planned
    validate_apply(
        plan=plan,
        playbook=PLAYBOOK,
        inventory=INVENTORY,
        spine=spine,
        removal_approval=None,
    )
