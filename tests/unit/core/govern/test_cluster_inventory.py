"""Cluster-scoped governance by declaration (issue #4988).

The properties under test, each named for what it protects:

1. test_labelled_workload_is_inventoried_without_mutating_its_manifest
2. test_unlabelled_workload_is_reported_ungoverned_not_omitted
3. test_explicit_opt_out_label_is_recorded_not_dropped
4. test_unrecognised_label_value_is_rejected_not_read_as_ungoverned
5. test_inventory_is_identical_regardless_of_listing_order
6. test_removing_the_label_emits_an_opt_out_transition
7. test_deleted_workload_emits_withdrawn_not_opt_out
8. test_new_labelled_workload_is_enrolled_without_a_manifest_edit
9. test_unchanged_workload_emits_no_transition
10. test_governed_workload_telemetry_is_anchored_under_its_own_source_label
11. test_opted_out_workload_telemetry_is_refused_by_the_route
12. test_inventory_feeds_the_playbook_diff_without_translation
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.govern import compute_plan
from bernstein.core.govern.cluster_inventory import (
    GOVERN_LABEL,
    ClusterGovernanceError,
    GovernanceState,
    TransitionKind,
    build_inventory,
    diff_workloads,
    inventory_workloads,
    route_workload_telemetry,
    telemetry_source_label,
)
from bernstein.core.govern.plan_models import PlanEntryKind

FIXTURE = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "cluster" / "agent-workloads.json"


def _fixture_items() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["items"])


def _workload(
    name: str,
    *,
    namespace: str = "agents",
    kind: str = "Deployment",
    uid: str | None = None,
    label: str | None = "enabled",
) -> dict[str, Any]:
    labels: dict[str, str] = {"app.kubernetes.io/name": name}
    if label is not None:
        labels[GOVERN_LABEL] = label
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": uid or f"uid-{name}",
            "labels": labels,
        },
    }


def _by_name(workloads: Any, name: str) -> Any:
    for w in workloads:
        if w.name == name:
            return w
    raise AssertionError(f"{name} missing from inventory")


@pytest.fixture
def audit_chain(tmp_path: Path) -> tuple[Path, bytes]:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir, b"k" * 32


def _genai_span(span_id: str = "f1e2d3c4b5a69788") -> dict[str, Any]:
    return {
        "traceId": "abc123def456abc123def456abc123de",
        "spanId": span_id,
        "name": "gen_ai.chat",
        "kind": "SPAN_KIND_CLIENT",
        "attributes": [
            {"key": "gen_ai.system", "value": {"stringValue": "anthropic"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
        ],
    }


# 1 --------------------------------------------------------------------------


def test_labelled_workload_is_inventoried_without_mutating_its_manifest() -> None:
    """Governance by declaration reads manifests; it never writes them back."""
    items = _fixture_items()
    pristine = copy.deepcopy(items)

    workloads = inventory_workloads(items)

    governed = _by_name(workloads, "research-agent")
    assert governed.state is GovernanceState.GOVERNED
    assert governed.namespace == "agents"
    assert governed.kind == "Deployment"
    assert governed.telemetry_source_label == telemetry_source_label("agents", "Deployment", "research-agent")
    assert items == pristine


# 2 --------------------------------------------------------------------------


def test_unlabelled_workload_is_reported_ungoverned_not_omitted() -> None:
    """A workload nobody labelled stays visible, marked ungoverned."""
    workloads = inventory_workloads(_fixture_items())

    names = {w.name for w in workloads}
    assert names == {"research-agent", "scratch-agent", "batch-agent"}

    scratch = _by_name(workloads, "scratch-agent")
    assert scratch.state is GovernanceState.UNGOVERNED
    assert scratch.label_value is None


# 3 --------------------------------------------------------------------------


def test_explicit_opt_out_label_is_recorded_not_dropped() -> None:
    """An opted-out workload is inventoried as opted out, not deleted."""
    workloads = inventory_workloads(_fixture_items())

    batch = _by_name(workloads, "batch-agent")
    assert batch.state is GovernanceState.OPTED_OUT
    assert batch.label_value == "disabled"


# 4 --------------------------------------------------------------------------


def test_unrecognised_label_value_is_rejected_not_read_as_ungoverned() -> None:
    """A declaration that cannot be read must not resolve to a posture."""
    with pytest.raises(ClusterGovernanceError) as exc:
        inventory_workloads([_workload("typo-agent", label="enabledd")])

    assert "typo-agent" in str(exc.value)
    assert "enabledd" in str(exc.value)


# 5 --------------------------------------------------------------------------


def test_inventory_is_identical_regardless_of_listing_order() -> None:
    """Two operators listing the same cluster get the same inventory hash."""
    items = _fixture_items()
    forward = build_inventory(inventory_workloads(items))
    reversed_ = build_inventory(inventory_workloads(list(reversed(items))))

    assert forward.content_hash() == reversed_.content_hash()


# 6 --------------------------------------------------------------------------


def test_removing_the_label_emits_an_opt_out_transition() -> None:
    """Dropping the label is an event, not a silent disappearance."""
    before = inventory_workloads([_workload("research-agent", label="enabled")])
    after = inventory_workloads([_workload("research-agent", label=None)])

    transitions = diff_workloads(before, after)

    assert [t.kind for t in transitions] == [TransitionKind.OPTED_OUT]
    only = transitions[0]
    assert only.workload_ref == "agents/Deployment/research-agent"
    assert only.previous_state is GovernanceState.GOVERNED
    assert only.current_state is GovernanceState.UNGOVERNED


# 7 --------------------------------------------------------------------------


def test_deleted_workload_emits_withdrawn_not_opt_out() -> None:
    """A workload that is gone is reported as gone, distinctly from opting out."""
    before = inventory_workloads([_workload("research-agent"), _workload("batch-agent")])
    after = inventory_workloads([_workload("research-agent")])

    transitions = diff_workloads(before, after)

    assert [t.kind for t in transitions] == [TransitionKind.WITHDRAWN]
    assert transitions[0].workload_ref == "agents/Deployment/batch-agent"
    assert transitions[0].current_state is None


# 8 --------------------------------------------------------------------------


def test_new_labelled_workload_is_enrolled_without_a_manifest_edit() -> None:
    """A workload that arrives already labelled inherits the posture."""
    before = inventory_workloads([_workload("research-agent")])
    after_items = [_workload("research-agent"), _workload("new-agent", label="enabled")]
    pristine = copy.deepcopy(after_items)
    after = inventory_workloads(after_items)

    transitions = diff_workloads(before, after)

    assert [t.kind for t in transitions] == [TransitionKind.ENROLLED]
    assert transitions[0].workload_ref == "agents/Deployment/new-agent"
    assert transitions[0].previous_state is None
    assert after_items == pristine


# 9 --------------------------------------------------------------------------


def test_unchanged_workload_emits_no_transition() -> None:
    """A steady cluster produces an empty transition list."""
    snapshot = inventory_workloads(_fixture_items())

    assert diff_workloads(snapshot, snapshot) == ()


# 10 -------------------------------------------------------------------------


def test_governed_workload_telemetry_is_anchored_under_its_own_source_label(
    audit_chain: tuple[Path, bytes],
) -> None:
    """The route carries workload identity into the signed ingest receipt."""
    audit_dir, hmac_key = audit_chain
    governed = _by_name(inventory_workloads(_fixture_items()), "research-agent")

    receipt = route_workload_telemetry(
        governed,
        [_genai_span()],
        audit_dir=audit_dir,
        hmac_key=hmac_key,
    )

    assert receipt.source_label == "k8s:agents/Deployment/research-agent"
    assert receipt.span_count == 1
    assert receipt.signature
    assert receipt.chain_entry_hash


# 11 -------------------------------------------------------------------------


def test_opted_out_workload_telemetry_is_refused_by_the_route(
    audit_chain: tuple[Path, bytes],
) -> None:
    """Opting out stops ingest; it does not silently keep ingesting."""
    audit_dir, hmac_key = audit_chain
    opted_out = _by_name(inventory_workloads(_fixture_items()), "batch-agent")

    with pytest.raises(ClusterGovernanceError) as exc:
        route_workload_telemetry(
            opted_out,
            [_genai_span()],
            audit_dir=audit_dir,
            hmac_key=hmac_key,
        )

    assert "opted_out" in str(exc.value)


# 12 -------------------------------------------------------------------------


def test_inventory_feeds_the_playbook_diff_without_translation() -> None:
    """The cluster inventory is the govern-plan inventory, not a parallel one."""
    inventory = build_inventory(inventory_workloads(_fixture_items()))

    plan = compute_plan(
        playbook={
            "forbidden": [
                {
                    "surface": "k8s:agents/Deployment/scratch-agent",
                    "clause": "no ad-hoc agent workloads in the agents namespace",
                }
            ],
            "required": [
                {
                    "surface": "k8s:agents/Deployment/audit-agent",
                    "clause": "the audit agent runs in every cluster",
                    "declared_value": "governed",
                }
            ],
        },
        inventory=inventory.to_dict(),
        run_id="cluster-govern",
        timestamp=1_788_000_000,
    )

    seen = {e.surface: (e.kind, e.observed_value) for e in plan.entries}
    assert seen["k8s:agents/Deployment/scratch-agent"] == (PlanEntryKind.FORBIDDEN, "ungoverned")
    assert seen["k8s:agents/Deployment/audit-agent"][0] is PlanEntryKind.ABSENT
