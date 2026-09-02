"""Governance coverage is a projection over recorded evidence (#5067, slice 2).

The screen this backs leads with what the installation *cannot* prove, so the
projection has to be wrong in the honest direction: an action the chain records
but cannot tie to a principal must lower the ratio, and the governance record's
own rows must not raise it.

Each test names the property it protects:

* an unattributed action is counted, not skipped;
* a principal the run made a verdict about makes its actions attributable;
* a denial attributes but does not authorise;
* decision records and chain bookkeeping are not agent actions;
* an empty run reports an absent ratio rather than 0 or 1;
* a tampered chain is reported as tampered rather than scored as clean.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.spine import (
    ARTIFACT_ATTEMPT_STEP_PREFIX,
    JOURNAL_SEAL_STEP_PREFIX,
    LineageSpine,
)
from bernstein.core.security.governance import RoleBindings, decide_access
from bernstein.core.security.governance_coverage import (
    GovernanceCoverage,
    collect_governance_coverage,
)

_KEY = b"0" * 32
_RUN = "run-1"


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _bindings() -> RoleBindings:
    return RoleBindings(
        group_to_role={"eng": "operator", "readers": "viewer"},
        role_permissions={"operator": ("tasks:write",), "viewer": ("costs:read",)},
    ).sign(_KEY)


def _act(workdir: Path, *, actor: str, path: str, step_id: str = "write", ts: int = 1000) -> None:
    LineageSpine(_lineage_root(workdir), run_id=_RUN, hmac_key=_KEY).record(
        artifact_path=path,
        content=path.encode(),
        actor=actor,
        step_id=step_id,
        model="none",
        timestamp=ts,
    )


def _decide(workdir: Path, *, subject: str, groups: tuple[str, ...], action: str) -> str:
    return decide_access(
        run_id=_RUN,
        lineage_root=_lineage_root(workdir),
        hmac_key=_KEY,
        subject=subject,
        idp_groups=groups,
        action=action,
        bindings=_bindings(),
        now=1000,
    ).verdict


def _coverage(workdir: Path) -> GovernanceCoverage:
    return collect_governance_coverage(workdir, _RUN, hmac_key=_KEY)


def test_action_with_no_named_principal_is_counted_and_not_attributable(tmp_path: Path) -> None:
    _act(tmp_path, actor="agent-writer", path="src/a.py")

    coverage = _coverage(tmp_path)

    assert coverage.recorded_actions == 1
    assert coverage.attributable_actions.covered == 0
    assert coverage.attributable_actions.total == 1
    assert coverage.attributable_actions.ratio == 0.0


def test_actor_named_as_a_decision_subject_is_attributable(tmp_path: Path) -> None:
    _act(tmp_path, actor="alice", path="src/a.py")
    _act(tmp_path, actor="agent-writer", path="src/b.py")
    assert _decide(tmp_path, subject="alice", groups=("eng",), action="tasks:write") == "allow"

    coverage = _coverage(tmp_path)

    assert coverage.recorded_actions == 2
    assert coverage.attributable_actions.covered == 1
    assert coverage.attributable_actions.ratio == 0.5


def test_denied_principal_is_attributable_but_not_decision_covered(tmp_path: Path) -> None:
    _act(tmp_path, actor="bob", path="src/a.py")
    assert _decide(tmp_path, subject="bob", groups=("readers",), action="tasks:write") == "deny"

    coverage = _coverage(tmp_path)

    assert coverage.attributable_actions.covered == 1
    assert coverage.decided_actions.covered == 0
    assert coverage.decided_actions.ratio == 0.0


def test_governance_decision_records_are_not_counted_as_agent_actions(tmp_path: Path) -> None:
    _act(tmp_path, actor="alice", path="src/a.py")
    for action in ("tasks:write", "costs:read"):
        _decide(tmp_path, subject="alice", groups=("eng",), action=action)

    coverage = _coverage(tmp_path)

    # Two decision rows landed in the same spine. Counting them as actions
    # would let the run raise its own coverage by recording more decisions.
    assert coverage.recorded_decisions == 2
    assert coverage.recorded_actions == 1
    assert coverage.attributable_actions.ratio == 1.0


def test_journal_seal_and_attempt_rows_are_not_counted_as_agent_actions(tmp_path: Path) -> None:
    _act(tmp_path, actor="agent-writer", path="src/a.py")
    _act(tmp_path, actor="agent-writer", path="run.journal", step_id=f"{JOURNAL_SEAL_STEP_PREFIX}deadbeef")
    _act(tmp_path, actor="agent-writer", path="src/missing.py", step_id=f"{ARTIFACT_ATTEMPT_STEP_PREFIX}t-1")

    coverage = _coverage(tmp_path)

    assert coverage.recorded_actions == 1


def test_run_with_no_recorded_actions_reports_an_absent_ratio(tmp_path: Path) -> None:
    coverage = collect_governance_coverage(tmp_path, "never-ran", hmac_key=_KEY)

    assert coverage.recorded_actions == 0
    assert coverage.attributable_actions.ratio is None
    assert coverage.decided_actions.ratio is None
    assert coverage.chain_status == "no_entries"
    assert coverage.to_dict()["metrics"]["attributable_action_ratio"]["ratio"] is None


def test_tampered_chain_is_reported_rather_than_scored_as_clean(tmp_path: Path) -> None:
    _act(tmp_path, actor="alice", path="src/a.py")
    _decide(tmp_path, subject="alice", groups=("eng",), action="tasks:write")
    spine_path = _lineage_root(tmp_path) / _RUN / "spine.jsonl"
    spine_path.write_bytes(spine_path.read_bytes().replace(b'"alice"', b'"mallory"'))

    coverage = _coverage(tmp_path)

    assert coverage.chain_status == "tampered"
