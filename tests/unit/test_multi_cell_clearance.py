"""Multi-cell tick wiring for BLOCKER clearance gates (#2556).

When a clearance coordinator is wired, a posted ``blocker`` is materialized into
an audit-anchored clearance gate during the tick instead of being merely logged.
Without a coordinator, the tick stays observe-only (regression, AC5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from bernstein.core.models import OrchestratorConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.communication.signal_actions import (
    ClearanceGateCoordinator,
    InMemoryClearanceInjector,
)
from bernstein.core.orchestration.multi_cell import MultiCellOrchestrator
from bernstein.core.security.audit_chain import EVENT_SIGNAL_GATE_PROJECTION, AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path


def _orchestrator(
    tmp_path: Path, board: BulletinBoard, coord: ClearanceGateCoordinator | None
) -> MultiCellOrchestrator:
    return MultiCellOrchestrator(
        config=OrchestratorConfig(server_url="http://localhost:9999"),
        spawner=MagicMock(spec=AgentSpawner),
        workdir=tmp_path,
        bulletin=board,
        clearance_coordinator=coord,
    )


def test_tick_materializes_blocker_when_coordinator_wired(tmp_path: Path) -> None:
    board = BulletinBoard()
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    orch = _orchestrator(tmp_path, board, coord)

    board.post(BulletinMessage(agent_id="w", type="blocker", content="dep broke", timestamp=1.0, cell_id="cell-a"))
    result = orch.tick()

    assert result.blockers_found == 1
    assert len(injector.created) == 1
    assert chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)
    assert any("clearance gate" in action for action in result.vp_actions)


def test_tick_observe_only_without_coordinator(tmp_path: Path) -> None:
    board = BulletinBoard()
    orch = _orchestrator(tmp_path, board, None)

    board.post(BulletinMessage(agent_id="w", type="blocker", content="dep broke", timestamp=1.0, cell_id="cell-a"))
    result = orch.tick()

    # Blocker is counted and logged, but nothing is materialized (legacy path).
    assert result.blockers_found == 1
    assert not any("clearance gate" in action for action in result.vp_actions)
