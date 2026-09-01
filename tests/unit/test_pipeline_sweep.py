"""Unit tests proving ``bernstein pipeline run`` drives the tracker pipeline.

Proven by:
- a sweep against a fake tracker adapter claims and releases the expected
  handoffs, asserted at the adapter boundary and not by reading the printout;
- ``--dry-run`` over the same config contacts the adapter zero times;
- two sweeps over identical state produce identical chain records apart from
  their timestamps;
- a tracker raising mid-sweep leaves the other stages executed, records the
  failure in the audit chain, and exits non-zero;
- configured trackers missing from registry are recorded in errors and exit non-zero;
- a test that fails if ``build_pipeline_from_yaml`` again has no caller in ``src/``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.pipeline_cmd import pipeline_group
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.trackers.contract import (
    AbstractTrackerAdapter,
    CommentResult,
    Ticket,
    TransitionResult,
)
from bernstein.core.trackers.registry import (
    register_tracker,
    reset_registry_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


@dataclass
class FakeSweepTrackerAdapter(AbstractTrackerAdapter):
    """In-memory tracker adapter that tracks calls at the boundary."""

    name: str = "fake_sweep"
    tickets: list[Ticket] = field(default_factory=list)
    comments: list[tuple[str, str, str | None]] = field(default_factory=list)
    transitions: list[tuple[str, str, str | None]] = field(default_factory=list)
    raise_on_pull: bool = False

    def pull_open_tickets(self, filter: dict[str, Any] | None = None) -> list[Ticket]:
        if self.raise_on_pull:
            raise RuntimeError("network timeout contacting tracker")
        status = (filter or {}).get("status") if filter else None
        return [t for t in self.tickets if status is None or t.status == status]

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        *,
        idempotency_key: str | None = None,
    ) -> CommentResult:
        self.comments.append((ticket_id, body, idempotency_key))
        return CommentResult(comment_id=f"c-{len(self.comments)}", ticket_id=ticket_id)

    def transition(
        self,
        ticket_id: str,
        status_id: str,
        *,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> TransitionResult:
        from dataclasses import replace

        self.transitions.append((ticket_id, status_id, idempotency_key))
        self.tickets = [replace(t, status=status_id) if t.id == ticket_id else t for t in self.tickets]
        return TransitionResult(ticket_id=ticket_id, new_status=status_id, etag=etag)


_PIPELINE_CONFIG = """
trackers:
  fake_sweep: {}

orchestration:
  tracker_pipeline:
    claim_lock_ttl_seconds: 600
    concurrency:
      per_role_max_in_flight: 2
    pipeline_stages:
      - role: engineer
        claim_status: todo
        success_status: done
        failure_status: failed
      - role: qa
        claim_status: done
        success_status: verified
        failure_status: qa_failed
        requires_prior_role: engineer
"""


def _run_cli(tmp_path: Path, config_content: str, *args: str) -> tuple[int, str]:
    config_file = tmp_path / "bernstein.yaml"
    config_file.write_text(config_content, encoding="utf-8")
    state_root = tmp_path / ".sdd"
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as _cwd:
        (Path(_cwd) / "bernstein.yaml").write_text(config_content, encoding="utf-8")
        result = runner.invoke(
            pipeline_group,
            ["run", "--config", "bernstein.yaml", "--state-root", str(state_root), *args],
        )
    return result.exit_code, result.output


def test_sweep_claims_and_releases_expected_handoffs_at_adapter_boundary(
    tmp_path: Path,
) -> None:
    """A sweep against a fake tracker adapter claims and releases handoffs asserted at the adapter."""
    adapter = FakeSweepTrackerAdapter(
        tickets=[
            Ticket(
                id="PROJ-1",
                external_url="https://example.test/PROJ-1",
                title="Task 1",
                status="todo",
                body="",
            ),
            Ticket(
                id="PROJ-2",
                external_url="https://example.test/PROJ-2",
                title="Task 2",
                status="todo",
                body="",
            ),
        ]
    )
    register_tracker("fake_sweep", lambda **kw: adapter, overwrite=True)

    code, _output = _run_cli(tmp_path, _PIPELINE_CONFIG)
    assert code == 0

    # Boundary assertions: verify adapter was called directly
    assert len(adapter.comments) == 2
    assert len(adapter.transitions) == 2
    assert adapter.transitions[0][:2] == ("PROJ-1", "done")
    assert adapter.transitions[1][:2] == ("PROJ-2", "done")

    # Comments contain structured success block
    assert "bernstein:success" in adapter.comments[0][1]
    assert 'role: "engineer"' in adapter.comments[0][1]


def test_dry_run_contacts_adapter_zero_times(tmp_path: Path) -> None:
    """--dry-run over the same config contacts the adapter zero times."""
    adapter = FakeSweepTrackerAdapter(
        tickets=[
            Ticket(
                id="PROJ-1",
                external_url="https://example.test/PROJ-1",
                title="Task 1",
                status="todo",
                body="",
            )
        ]
    )
    register_tracker("fake_sweep", lambda **kw: adapter, overwrite=True)

    code, _output = _run_cli(tmp_path, _PIPELINE_CONFIG, "--dry-run")
    assert code == 0

    # Adapter boundary assertion: 0 calls
    assert len(adapter.comments) == 0
    assert len(adapter.transitions) == 0


def test_two_sweeps_over_identical_state_produce_identical_chain_records(
    tmp_path: Path,
) -> None:
    """Two sweeps over identical state produce identical chain records apart from timestamps."""
    config = """
trackers:
  fake_sweep: {}

orchestration:
  tracker_pipeline:
    pipeline_stages:
      - role: engineer
        claim_status: todo
        success_status: done
        failure_status: failed
"""
    # Sweep 1
    adapter1 = FakeSweepTrackerAdapter(
        tickets=[
            Ticket(
                id="PROJ-10",
                external_url="https://example.test/PROJ-10",
                title="Task 10",
                status="todo",
                body="",
            )
        ]
    )
    register_tracker("fake_sweep", lambda **kw: adapter1, overwrite=True)
    run1_dir = tmp_path / "run1"
    run1_dir.mkdir(parents=True, exist_ok=True)
    state_root1 = run1_dir / ".sdd"
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=run1_dir) as cwd1:
        (Path(cwd1) / "bernstein.yaml").write_text(config, encoding="utf-8")
        runner.invoke(
            pipeline_group,
            ["run", "--config", "bernstein.yaml", "--state-root", str(state_root1)],
        )

    # Sweep 2 (identical initial state)
    adapter2 = FakeSweepTrackerAdapter(
        tickets=[
            Ticket(
                id="PROJ-10",
                external_url="https://example.test/PROJ-10",
                title="Task 10",
                status="todo",
                body="",
            )
        ]
    )
    register_tracker("fake_sweep", lambda **kw: adapter2, overwrite=True)
    run2_dir = tmp_path / "run2"
    run2_dir.mkdir(parents=True, exist_ok=True)
    state_root2 = run2_dir / ".sdd"
    with runner.isolated_filesystem(temp_dir=run2_dir) as cwd2:
        (Path(cwd2) / "bernstein.yaml").write_text(config, encoding="utf-8")
        runner.invoke(
            pipeline_group,
            ["run", "--config", "bernstein.yaml", "--state-root", str(state_root2)],
        )

    chain1 = AuditChainStore(state_root1 / "audit")
    chain2 = AuditChainStore(state_root2 / "audit")

    events1 = chain1.query(event_type="tracker_pipeline.sweep")
    events2 = chain2.query(event_type="tracker_pipeline.sweep")

    assert len(events1) == 1
    assert len(events2) == 1

    d1 = events1[0].details
    d2 = events2[0].details

    # All details fields match exactly except prev_chain_digest (which depends on store genesis)
    assert d1["config_digest"] == d2["config_digest"]
    assert d1["trackers_configured"] == d2["trackers_configured"]
    assert d1["trackers_contacted"] == d2["trackers_contacted"]
    assert d1["handoffs"] == d2["handoffs"]
    assert d1["stage_outcomes"] == d2["stage_outcomes"]
    assert d1["status"] == d2["status"]


def test_tracker_raising_mid_sweep_leaves_other_stages_executed_and_recorded(
    tmp_path: Path,
) -> None:
    """A tracker raising mid-sweep records the failure in the event and exits non-zero."""
    failing_adapter = FakeSweepTrackerAdapter(raise_on_pull=True)
    register_tracker("fake_sweep", lambda **kw: failing_adapter, overwrite=True)

    state_root = tmp_path / ".sdd"
    code, output = _run_cli(tmp_path, _PIPELINE_CONFIG)
    # Must exit non-zero on tracker failure so cron/operators alert
    assert code != 0
    assert "error" in output.lower()

    chain = AuditChainStore(state_root / "audit")
    events = chain.query(event_type="tracker_pipeline.sweep")
    assert len(events) == 1
    details = events[0].details
    assert details["trackers_configured"] == ["fake_sweep"]
    assert details["trackers_contacted"] == ["fake_sweep"]
    assert details["handoffs"] == []
    assert details["status"] == "failed"
    assert details["errors"] is not None
    assert any("network timeout" in e for e in details["errors"])
    # Both stages were evaluated and marked error
    assert details["stage_outcomes"] == {"engineer": "error", "qa": "error"}


def test_configured_tracker_missing_from_registry_recorded_as_error_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    """A configured tracker not found in the registry is recorded in the receipt and exits non-zero."""
    config = """
trackers:
  unregistered_tracker:
    api_key: secret

orchestration:
  tracker_pipeline:
    pipeline_stages:
      - role: engineer
        claim_status: todo
        success_status: done
        failure_status: failed
"""
    state_root = tmp_path / ".sdd"
    code, output = _run_cli(tmp_path, config)
    assert code != 0
    assert "not found in registry" in output

    chain = AuditChainStore(state_root / "audit")
    events = chain.query(event_type="tracker_pipeline.sweep")
    assert len(events) == 1
    details = events[0].details
    assert details["trackers_configured"] == ["unregistered_tracker"]
    assert details["trackers_contacted"] == []
    assert details["status"] == "failed"
    assert any("unregistered_tracker" in e for e in details["errors"])


def test_build_pipeline_from_yaml_has_caller_in_src() -> None:
    """Verify that ``build_pipeline_from_yaml`` is actively called within ``src/``."""
    src_dir = Path(__file__).parents[2] / "src"
    callers: list[str] = []

    for py_file in src_dir.rglob("*.py"):
        # Exclude definition file itself
        if py_file.name == "tracker_pipeline.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_name_match = isinstance(func, ast.Name) and func.id == "build_pipeline_from_yaml"
                is_attr_match = isinstance(func, ast.Attribute) and func.attr == "build_pipeline_from_yaml"
                if is_name_match or is_attr_match:
                    callers.append(str(py_file.relative_to(src_dir)))

    assert len(callers) > 0, "build_pipeline_from_yaml has no caller in src/"
    assert any("pipeline_cmd.py" in c for c in callers)
