"""Catalog-wide integrity checks across workspace and wheel-packaged templates."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.core.planning.routine_bridge import build_task_payloads, estimate_minutes, spawn_scenario_tasks
from bernstein.core.planning.routine_provisioner import build_routine_prompt
from bernstein.core.planning.scenario_library import load_scenario_library
from bernstein.core.planning.workflow import WorkflowDefinition, WorkflowPhase
from bernstein.core.planning.workflow_dsl import (
    ConditionExpr,
    DAGEdge,
    DAGExecutor,
    DAGNode,
    WorkflowDAG,
    parse_workflow_yaml,
)
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.teams.drift import compute_role_digests
from bernstein.core.workflows.workflow_runner import NodeStatus, WorkflowRunner
from bernstein.core.workflows.workflow_spec import load_workflow_spec, load_workflow_spec_from_text

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "pyproject.toml").is_file(),
    reason="template catalog checks only run in a source checkout",
)


def _dsl_task(node_id: str, status: TaskStatus, role: str = "backend") -> Task:
    return Task(id=node_id, title=node_id, description=node_id, role=role, status=status)


def _assert_rework_branch_reverified_and_reaches_publish(
    dag: WorkflowDAG,
    *,
    initial_check: str,
    rework_node: str,
    publish_node: str,
) -> str:
    """Assert that a failed-check rework branch re-runs verification and can reach publish.

    Fails if:
      - ``rework_node`` has no downstream post-fix verification node, OR
      - ``publish_node`` is gated solely on the original failed ``initial_check``
        and cannot become ready after ``rework_node`` + post-fix verification pass, OR
      - ``publish_node`` cannot become ready on the clean first-pass happy path, OR
      - ``publish_node`` becomes ready when post-fix verification fails.
    """
    outgoing: dict[str, list[DAGEdge]] = {}
    for edge in dag.edges:
        outgoing.setdefault(edge.source, []).append(edge)

    reverify_candidates = [e.target for e in outgoing.get(rework_node, []) if e.target != publish_node]
    assert reverify_candidates, (
        f"Workflow {dag.definition.name!r}: rework node {rework_node!r} has no post-fix verification node"
    )
    reverify_node = reverify_candidates[0]

    publish_sources = {e.source for e in dag.edges if e.target == publish_node}
    assert reverify_node in publish_sources, (
        f"Workflow {dag.definition.name!r}: publish node {publish_node!r} is not gated on "
        f"post-fix check {reverify_node!r} (sources={sorted(publish_sources)})"
    )

    executor = DAGExecutor(dag)

    # Seed every ancestor up to initial_check as DONE.
    def _done_ancestors(target: str) -> dict[str, Task]:
        ancestors: set[str] = set()
        stack = [target]
        while stack:
            cur = stack.pop()
            for edge in dag.edges:
                if edge.target == cur and edge.source not in ancestors:
                    ancestors.add(edge.source)
                    stack.append(edge.source)
        return {nid: _dsl_task(nid, TaskStatus.DONE, role=dag.node_map[nid].role) for nid in ancestors}

    # 1. Clean first-pass happy path -> publish becomes ready without running rework.
    happy_tasks = _done_ancestors(initial_check)
    happy_tasks[initial_check] = _dsl_task(initial_check, TaskStatus.DONE)
    happy_ready = executor.ready_nodes(happy_tasks)
    assert publish_node in happy_ready, (
        f"Workflow {dag.definition.name!r}: {publish_node!r} not ready on first-pass success ({happy_ready})"
    )
    assert rework_node not in happy_ready

    # 2. Rework path: initial check fails -> rework -> post-fix check -> publish.
    rework_tasks = _done_ancestors(initial_check)
    rework_tasks[initial_check] = _dsl_task(initial_check, TaskStatus.FAILED)
    ready_after_fail = executor.ready_nodes(rework_tasks)
    assert rework_node in ready_after_fail
    assert publish_node not in ready_after_fail

    rework_tasks[rework_node] = _dsl_task(rework_node, TaskStatus.DONE)
    ready_after_rework = executor.ready_nodes(rework_tasks)
    assert reverify_node in ready_after_rework
    assert publish_node not in ready_after_rework

    # 3. Post-fix check fails -> publish MUST stay blocked.
    failed_reverify_tasks = dict(rework_tasks)
    failed_reverify_tasks[reverify_node] = _dsl_task(reverify_node, TaskStatus.FAILED)
    assert publish_node not in executor.ready_nodes(failed_reverify_tasks)

    # 4. Post-fix check passes -> publish becomes ready.
    rework_tasks[reverify_node] = _dsl_task(reverify_node, TaskStatus.DONE)
    assert publish_node in executor.ready_nodes(rework_tasks), (
        f"Workflow {dag.definition.name!r}: {publish_node!r} still unreachable after "
        f"{rework_node!r} and {reverify_node!r} succeeded"
    )
    return reverify_node


def _assert_dr_dsl_teardown_runs_on_upstream_failure(
    dag: WorkflowDAG,
    *,
    provision_node: str,
    restore_node: str,
    verify_node: str,
    receipt_node: str,
    cleanup_nodes: set[str],
) -> None:
    """Assert teardown/cleanup cannot be skipped when provisioning, restore, or verify fails, while receipt stays gated."""
    executor = DAGExecutor(dag)

    # Case 0: Provisioning fails (may leave partial sandbox) -> cleanup runs; receipt never runs.
    provision_fail_tasks = {
        provision_node: _dsl_task(provision_node, TaskStatus.FAILED),
    }
    ready_on_provision_fail = set(executor.ready_nodes(provision_fail_tasks))
    assert ready_on_provision_fail & cleanup_nodes, (
        f"DR workflow {dag.definition.name!r} skipped teardown when {provision_node!r} failed "
        f"(ready={sorted(ready_on_provision_fail)})"
    )
    assert receipt_node not in ready_on_provision_fail
    for cleanup_id in ready_on_provision_fail & cleanup_nodes:
        provision_fail_tasks[cleanup_id] = _dsl_task(cleanup_id, TaskStatus.DONE)
    assert receipt_node not in executor.ready_nodes(provision_fail_tasks)

    # Case 1: Restore fails -> cleanup runs immediately AND after materialize_unreachable; receipt never runs.
    restore_fail_tasks = {
        provision_node: _dsl_task(provision_node, TaskStatus.DONE),
        restore_node: _dsl_task(restore_node, TaskStatus.FAILED),
    }
    ready_on_restore_fail = set(executor.ready_nodes(restore_fail_tasks))
    assert ready_on_restore_fail & cleanup_nodes, (
        f"DR workflow {dag.definition.name!r} skipped teardown when {restore_node!r} failed "
        f"(ready={sorted(ready_on_restore_fail)})"
    )
    assert receipt_node not in ready_on_restore_fail
    for cleanup_id in ready_on_restore_fail & cleanup_nodes:
        restore_fail_tasks[cleanup_id] = _dsl_task(cleanup_id, TaskStatus.DONE)
    assert receipt_node not in executor.ready_nodes(restore_fail_tasks)

    # Case 2: Restore succeeds, verification fails -> cleanup runs; receipt never runs.
    verify_fail_tasks = {
        provision_node: _dsl_task(provision_node, TaskStatus.DONE),
        restore_node: _dsl_task(restore_node, TaskStatus.DONE),
        verify_node: _dsl_task(verify_node, TaskStatus.FAILED),
    }
    ready_on_verify_fail = set(executor.ready_nodes(verify_fail_tasks))
    assert ready_on_verify_fail & cleanup_nodes, (
        f"DR workflow {dag.definition.name!r} skipped teardown when {verify_node!r} failed "
        f"(ready={sorted(ready_on_verify_fail)})"
    )
    assert receipt_node not in ready_on_verify_fail
    for cleanup_id in ready_on_verify_fail & cleanup_nodes:
        verify_fail_tasks[cleanup_id] = _dsl_task(cleanup_id, TaskStatus.DONE)
    assert receipt_node not in executor.ready_nodes(verify_fail_tasks)

    # Case 3: Restore succeeds, verification still in progress -> cleanup MUST wait.
    in_flight_tasks = {
        provision_node: _dsl_task(provision_node, TaskStatus.DONE),
        restore_node: _dsl_task(restore_node, TaskStatus.DONE),
        verify_node: _dsl_task(verify_node, TaskStatus.IN_PROGRESS),
    }
    assert not (set(executor.ready_nodes(in_flight_tasks)) & cleanup_nodes), (
        f"DR workflow {dag.definition.name!r} triggered teardown while {verify_node!r} was still in progress"
    )


def test_all_workspace_and_packaged_scenarios_parse_cleanly() -> None:
    ws_root = REPO_ROOT / ".bernstein" / "scenarios"
    pkg_root = REPO_ROOT / "templates" / "scenarios"

    ws_files = sorted(ws_root.rglob("*.yaml"))
    pkg_files = sorted(pkg_root.glob("*.yaml"))
    assert len(ws_files) >= 38
    assert len(pkg_files) >= 24

    ws_lib = load_scenario_library(ws_root)
    pkg_lib = load_scenario_library(pkg_root)
    assert len(ws_lib.scenarios) == len(ws_files)
    assert len(pkg_lib.scenarios) == len(pkg_files)

    for sc_id, scenario in {**ws_lib.scenarios, **pkg_lib.scenarios}.items():
        assert scenario.tasks, f"Scenario {sc_id} has no tasks"


def test_all_workspace_conditional_workflows_dry_run_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf_dir = REPO_ROOT / ".bernstein" / "workflows"
    wf_files = sorted(wf_dir.glob("*.yaml"))
    assert len(wf_files) >= 16

    monkeypatch.chdir(REPO_ROOT)
    runner = CliRunner(env={"COLUMNS": "200"})
    listed = runner.invoke(cli, ["workflow", "list"])
    assert listed.exit_code == 0, listed.output

    for wf_file in wf_files:
        res = runner.invoke(cli, ["workflow", "run", wf_file.stem, "--dry-run"])
        assert res.exit_code == 0, f"{wf_file.name} failed dry-run:\n{res.output}"


def test_all_packaged_workflow_specs_and_curated_teams_validate() -> None:
    tpl_wf_dir = REPO_ROOT / "templates" / "workflows"
    tpl_wf_files = sorted(tpl_wf_dir.glob("*.yaml"))
    assert len(tpl_wf_files) >= 15

    for wf_file in tpl_wf_files:
        spec = load_workflow_spec_from_text(wf_file.read_text(encoding="utf-8"))
        assert spec.name == wf_file.stem
        assert spec.nodes

    roles_dir = REPO_ROOT / "templates" / "roles"
    curated_teams = (
        "fullstack",
        "security-audit",
        "research-ops",
        "ai-governance",
        "platform-sre",
        "data-migration",
    )
    for team_name in curated_teams:
        manifest_path = REPO_ROOT / "templates" / "teams" / f"{team_name}.toml"
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        role_names = [entry["role"] for entry in data["roles"]]
        expected_digests = compute_role_digests(roles_dir, role_names)
        assert data["role_template_digests"] == expected_digests, f"Digest drift in {team_name}.toml"


@pytest.mark.parametrize(
    ("filename", "initial_check", "rework_node", "publish_node"),
    [
        ("ai-governance-gate.yaml", "security-signoff", "rework-guardrails", "publish-aims-record"),
        ("customer-escalation-triage.yaml", "verify-fix-and-boundaries", "rework-escalation-fix", "draft-customer-rca"),
        (
            "finops-budget-remediation.yaml",
            "verify-quality-and-security-floors",
            "retune-routing",
            "publish-finops-report",
        ),
        ("zero-downtime-migration.yaml", "verify-row-hash-parity", "fix-migration-defect", "emit-cutover-runbook"),
    ],
)
def test_rework_workflows_reverify_before_publishing(
    filename: str,
    initial_check: str,
    rework_node: str,
    publish_node: str,
) -> None:
    dag = parse_workflow_yaml(REPO_ROOT / ".bernstein" / "workflows" / filename)
    _assert_rework_branch_reverified_and_reaches_publish(
        dag,
        initial_check=initial_check,
        rework_node=rework_node,
        publish_node=publish_node,
    )


def test_rework_guard_rejects_dead_end_rework_dag() -> None:
    """Regression guard: a publish node gated only on the original failed check must fail the check."""
    broken = WorkflowDAG(
        definition=WorkflowDefinition(
            name="broken-rework",
            phases=(
                WorkflowPhase(name="fix"),
                WorkflowPhase(name="verify"),
                WorkflowPhase(name="publish"),
            ),
        ),
        nodes=(
            DAGNode(id="implement", phase="fix", role="backend"),
            DAGNode(id="check", phase="verify", role="qa"),
            DAGNode(id="rework", phase="fix", role="backend"),
            DAGNode(id="publish", phase="publish", role="docs"),
        ),
        edges=(
            DAGEdge(source="implement", target="check"),
            DAGEdge(source="check", target="rework", condition=ConditionExpr(raw="status == 'failed'")),
            DAGEdge(source="check", target="publish", condition=ConditionExpr(raw="status == 'done'")),
        ),
    )
    with pytest.raises(AssertionError, match="no post-fix verification node"):
        _assert_rework_branch_reverified_and_reaches_publish(
            broken,
            initial_check="check",
            rework_node="rework",
            publish_node="publish",
        )


def test_dr_dsl_workflow_always_cleans_up_on_failure_and_gates_receipt() -> None:
    dag = parse_workflow_yaml(REPO_ROOT / ".bernstein" / "workflows" / "disaster-recovery-drill.yaml")
    _assert_dr_dsl_teardown_runs_on_upstream_failure(
        dag,
        provision_node="provision-isolated-target",
        restore_node="execute-runbook-restore",
        verify_node="verify-ledger-and-data-integrity",
        receipt_node="emit-dr-receipt",
        cleanup_nodes={"cleanup-sandbox-on-failure", "verify-sandbox-disposal"},
    )

    # Happy path: restore + verify -> verify-sandbox-disposal -> emit-dr-receipt.
    executor = DAGExecutor(dag)
    tasks = {
        "provision-isolated-target": _dsl_task("provision-isolated-target", TaskStatus.DONE),
        "execute-runbook-restore": _dsl_task("execute-runbook-restore", TaskStatus.DONE),
        "verify-ledger-and-data-integrity": _dsl_task("verify-ledger-and-data-integrity", TaskStatus.DONE),
    }
    ready = executor.ready_nodes(tasks)
    assert ready == ["verify-sandbox-disposal"]
    tasks["verify-sandbox-disposal"] = _dsl_task("verify-sandbox-disposal", TaskStatus.FAILED)
    assert "emit-dr-receipt" not in executor.ready_nodes(tasks)

    tasks["verify-sandbox-disposal"] = _dsl_task("verify-sandbox-disposal", TaskStatus.DONE)
    assert executor.ready_nodes(tasks) == ["emit-dr-receipt"]


def test_dr_teardown_guard_rejects_skippable_disposal_dag() -> None:
    """Regression guard: a DR workflow whose disposal depends unconditionally on verify must fail the check."""
    broken = WorkflowDAG(
        definition=WorkflowDefinition(
            name="broken-dr",
            phases=(
                WorkflowPhase(name="prepare"),
                WorkflowPhase(name="restore"),
                WorkflowPhase(name="verify"),
                WorkflowPhase(name="certify"),
            ),
        ),
        nodes=(
            DAGNode(id="provision", phase="prepare", role="devops"),
            DAGNode(id="restore", phase="restore", role="devops"),
            DAGNode(id="verify", phase="verify", role="qa"),
            DAGNode(id="dispose", phase="verify", role="security"),
            DAGNode(id="receipt", phase="certify", role="docs"),
        ),
        edges=(
            DAGEdge(source="provision", target="restore"),
            DAGEdge(source="restore", target="verify"),
            DAGEdge(source="verify", target="dispose"),
            DAGEdge(source="dispose", target="receipt", condition=ConditionExpr(raw="status == 'done'")),
        ),
    )
    with pytest.raises(AssertionError, match="skipped teardown"):
        _assert_dr_dsl_teardown_runs_on_upstream_failure(
            broken,
            provision_node="provision",
            restore_node="restore",
            verify_node="verify",
            receipt_node="receipt",
            cleanup_nodes={"dispose"},
        )


@pytest.mark.parametrize("failing_node", ["isolated-restore", "integrity-verification", "sandbox-disposal", None])
def test_packaged_dr_workflow_spec_always_runs_sandbox_disposal(
    tmp_path: Path,
    failing_node: str | None,
) -> None:
    spec = load_workflow_spec(REPO_ROOT / "templates" / "workflows" / "disaster-recovery-drill.yaml")
    disposal = spec.node_by_id("sandbox-disposal")
    receipt = spec.node_by_id("dr-receipt")
    assert disposal.always_run is True
    assert receipt.always_run is False

    workdir = tmp_path / f"dr-{failing_node or 'ok'}"
    workdir.mkdir()
    grant_workspace_trust(workdir)

    dispatched: list[str] = []

    class FakeSpawner:
        def spawn_for_tasks(self, tasks: list[Any]) -> Any:
            node_title = tasks[0].title.removeprefix("workflow:")
            dispatched.append(node_title)
            if node_title == failing_node:
                raise RuntimeError(f"simulated failure in {node_title}")
            session = MagicMock()
            session.id = f"sess-{node_title}"
            return session

    runner = WorkflowRunner(spawner=FakeSpawner(), workdir=workdir)  # type: ignore[arg-type]
    execution = runner.run(spec, goal="quarterly DR drill")
    by_id = {n.node_id: n for n in execution.nodes}

    assert "sandbox-disposal" in dispatched, f"sandbox-disposal was skipped when {failing_node} failed"
    if failing_node is None:
        assert execution.succeeded is True
        assert by_id["dr-receipt"].status == NodeStatus.SUCCESS
    else:
        assert execution.succeeded is False
        assert by_id["dr-receipt"].status == NodeStatus.SKIPPED
        assert "dr-receipt" not in dispatched
        if failing_node != "sandbox-disposal":
            assert by_id["sandbox-disposal"].status == NodeStatus.SUCCESS


def test_dr_scenario_is_sequenced_and_synced_between_workspace_and_packaged() -> None:
    pkg_path = REPO_ROOT / "templates" / "scenarios" / "disaster-recovery-failover-drill.yaml"
    ws_path = REPO_ROOT / ".bernstein" / "scenarios" / "ops" / "disaster-recovery-failover-drill.yaml"
    assert pkg_path.read_text(encoding="utf-8") == ws_path.read_text(encoding="utf-8")

    recipe = load_scenario_library(pkg_path.parent).get("disaster-recovery-failover-drill")
    assert recipe is not None
    assert len(recipe.tasks) >= 3
    assert [t.id for t in recipe.tasks] == ["inventory-backups", "execute-dr-drill", "audit-dr-receipt"]
    assert recipe.tasks[1].depends_on == ("inventory-backups",)
    assert recipe.tasks[2].depends_on == ("execute-dr-drill",)
    assert "disaster-recovery-drill.yaml" in recipe.tasks[1].description
    # Sequenced chain (15 + 75 + 15) must not receive a parallel role discount.
    assert estimate_minutes(recipe) == 105

    payloads = build_task_payloads(recipe)
    posted: list[dict[str, Any]] = []

    def fake_poster(_path: str, body: dict[str, Any]) -> dict[str, Any]:
        posted.append(body)
        return {"id": f"srv-task-{len(posted)}"}

    ids = spawn_scenario_tasks(payloads, poster=fake_poster)
    assert ids == ["srv-task-1", "srv-task-2", "srv-task-3"]
    assert "depends_on" not in posted[0]
    assert posted[1]["depends_on"] == ["srv-task-1"]
    assert posted[2]["depends_on"] == ["srv-task-2"]

    prompt = build_routine_prompt(recipe, "http://127.0.0.1:8052")
    assert "parallel tasks" not in prompt
    assert "respecting declared task dependencies" in prompt


def test_all_bundled_workflows_are_documented_in_operator_guide() -> None:
    guide = (REPO_ROOT / "docs" / "operations" / "workflows.md").read_text(encoding="utf-8")
    tpl_wf_files = sorted((REPO_ROOT / "templates" / "workflows").glob("*.yaml"))
    missing = [wf.name for wf in tpl_wf_files if wf.name not in guide]
    assert not missing, f"Bundled workflows missing from docs/operations/workflows.md: {missing}"
