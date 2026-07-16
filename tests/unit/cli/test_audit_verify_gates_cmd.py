"""Tests for ``bernstein audit verify-gates`` (#2556).

The forensic verifier reconstructs, from the audit chain alone, that no
dependent task was claimed while its clearance gate was open, and that every
recorded ``graph_delta_hash`` recomputes byte-identically.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.communication.signal_actions import (
    ClearanceGateCoordinator,
    InMemoryClearanceInjector,
)
from bernstein.core.security.audit import AUDIT_KEY_ENV
from bernstein.core.security.audit_chain import AuditChainStore, record_task_claim_receipt

AUDIT_DIR = Path(".sdd/audit")


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _materialize() -> str:
    """Materialize one clearance gate onto the isolated audit chain."""
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = AuditChainStore(AUDIT_DIR)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    blocker = board.post(
        BulletinMessage(agent_id="w", type="blocker", content="dep broke", timestamp=1.0, cell_id="cell-a")
    )
    spec = coord.materialize(blocker)
    assert spec is not None
    return spec.clearance_task_id


def test_verify_gates_passes_on_clean_chain(isolated_audit: Path) -> None:
    _materialize()  # a pending gate with no rogue claims verifies clean
    result = CliRunner().invoke(audit_group, ["verify-gates"])
    assert result.exit_code == 0, result.output
    assert "Clearance Gate Verification Passed" in result.output


def test_verify_gates_fails_on_claim_during_open_gate(isolated_audit: Path) -> None:
    clearance_id = _materialize()
    # A rogue claim of the scoped dependent while the gate is still open.
    chain = AuditChainStore(AUDIT_DIR)
    record_task_claim_receipt(
        chain=chain,
        task_id="task-x",
        role="backend",
        claimed_by="sess-rogue",
        depends_on=[clearance_id],
        task_version=2,
        claim_path="by_id",
    )

    result = CliRunner().invoke(audit_group, ["verify-gates"])
    assert result.exit_code == 1, result.output
    assert "Clearance Gate Verification FAILED" in result.output
    assert "task-x" in result.output


def test_verify_gates_noop_when_no_gates(isolated_audit: Path) -> None:
    # A directory with a chain but no gate projections passes silently.
    AuditChainStore(AUDIT_DIR).log(
        event_type="task.transition", actor="x", resource_type="task", resource_id="t1", details={}
    )
    result = CliRunner().invoke(audit_group, ["verify-gates"])
    assert result.exit_code == 0, result.output
