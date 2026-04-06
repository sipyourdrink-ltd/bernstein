"""Tests for MCP-013: ACP protocol integration for IDE agent communication."""

from __future__ import annotations

import time

import pytest

from bernstein.core.acp_ide_bridge import (
    ACPDiagnostic,
    ACPIdeBridge,
    DiagnosticSeverity,
    FileEditProposal,
    IDESession,
    IDESessionState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bridge() -> ACPIdeBridge:
    return ACPIdeBridge(stale_timeout=2.0)


@pytest.fixture()
def session(bridge: ACPIdeBridge) -> IDESession:
    return bridge.connect_ide("jetbrains-air", editor_info={"version": "2025.1"})


# ---------------------------------------------------------------------------
# Tests — Session management
# ---------------------------------------------------------------------------


class TestSessionManagement:
    def test_connect_ide(self, bridge: ACPIdeBridge) -> None:
        session = bridge.connect_ide("vscode")
        assert session.editor_name == "vscode"
        assert session.state == IDESessionState.CONNECTED
        assert session.id != ""

    def test_connect_with_info(self, bridge: ACPIdeBridge) -> None:
        session = bridge.connect_ide("zed", editor_info={"theme": "dark"})
        assert session.editor_info["theme"] == "dark"

    def test_disconnect(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        assert bridge.disconnect_ide(session.id) is True
        updated = bridge.get_session(session.id)
        assert updated is not None
        assert updated.state == IDESessionState.DISCONNECTED

    def test_disconnect_unknown(self, bridge: ACPIdeBridge) -> None:
        assert bridge.disconnect_ide("nonexistent") is False

    def test_heartbeat(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        old_hb = session.last_heartbeat
        time.sleep(0.01)
        assert bridge.heartbeat(session.id) is True
        assert session.last_heartbeat > old_hb

    def test_heartbeat_unknown(self, bridge: ACPIdeBridge) -> None:
        assert bridge.heartbeat("nonexistent") is False

    def test_list_connected(self, bridge: ACPIdeBridge) -> None:
        s1 = bridge.connect_ide("vscode")
        s2 = bridge.connect_ide("zed")
        bridge.disconnect_ide(s2.id)
        connected = bridge.list_connected()
        assert len(connected) == 1
        assert connected[0].id == s1.id


# ---------------------------------------------------------------------------
# Tests — Stale sessions
# ---------------------------------------------------------------------------


class TestStaleSessions:
    def test_mark_stale(self, bridge: ACPIdeBridge) -> None:
        session = bridge.connect_ide("vscode")
        # Manually set heartbeat to past
        session.last_heartbeat = time.time() - 10.0
        stale = bridge.mark_stale_sessions()
        assert session.id in stale
        assert session.state == IDESessionState.STALE

    def test_heartbeat_recovers_stale(self, bridge: ACPIdeBridge) -> None:
        session = bridge.connect_ide("vscode")
        session.last_heartbeat = time.time() - 10.0
        bridge.mark_stale_sessions()
        assert session.state == IDESessionState.STALE
        bridge.heartbeat(session.id)
        assert session.state == IDESessionState.CONNECTED


# ---------------------------------------------------------------------------
# Tests — Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    def test_push_diagnostic(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        diag = ACPDiagnostic(
            file_path="src/main.py",
            message="Unused import",
            severity=DiagnosticSeverity.WARNING,
            line=5,
            source="qa",
        )
        assert bridge.push_diagnostic(session.id, diag) is True
        assert session.notification_count == 1

    def test_push_to_disconnected(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        bridge.disconnect_ide(session.id)
        diag = ACPDiagnostic(file_path="f.py", message="error")
        assert bridge.push_diagnostic(session.id, diag) is False

    def test_push_to_all(self, bridge: ACPIdeBridge) -> None:
        bridge.connect_ide("vscode")
        bridge.connect_ide("zed")
        diag = ACPDiagnostic(file_path="f.py", message="info")
        count = bridge.push_diagnostic_to_all(diag)
        assert count == 2

    def test_diagnostic_to_dict(self) -> None:
        diag = ACPDiagnostic(
            file_path="f.py",
            message="err",
            severity=DiagnosticSeverity.ERROR,
            line=10,
            column=5,
            code="E001",
        )
        d = diag.to_dict()
        assert d["severity"] == "error"
        assert d["line"] == 10
        assert d["code"] == "E001"

    def test_diagnostic_to_dict_minimal(self) -> None:
        diag = ACPDiagnostic(file_path="f.py", message="info")
        d = diag.to_dict()
        assert "line" not in d
        assert "column" not in d
        assert "code" not in d


# ---------------------------------------------------------------------------
# Tests — File edit proposals
# ---------------------------------------------------------------------------


class TestFileEditProposals:
    def test_propose_edit(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        proposal = bridge.propose_edit(
            session.id,
            "src/main.py",
            "Add type annotation",
            old_content="def foo(x):",
            new_content="def foo(x: int):",
        )
        assert proposal is not None
        assert proposal.file_path == "src/main.py"
        assert proposal.accepted is None  # pending

    def test_propose_to_disconnected(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        bridge.disconnect_ide(session.id)
        assert bridge.propose_edit(session.id, "f.py", "test") is None

    def test_resolve_proposal_accept(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        proposal = bridge.propose_edit(session.id, "f.py", "test")
        assert proposal is not None
        resolved = bridge.resolve_proposal(proposal.id, accepted=True)
        assert resolved is not None
        assert resolved.accepted is True

    def test_resolve_proposal_reject(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        proposal = bridge.propose_edit(session.id, "f.py", "test")
        assert proposal is not None
        resolved = bridge.resolve_proposal(proposal.id, accepted=False)
        assert resolved is not None
        assert resolved.accepted is False

    def test_resolve_unknown(self, bridge: ACPIdeBridge) -> None:
        assert bridge.resolve_proposal("nonexistent", accepted=True) is None

    def test_get_pending_proposals(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        bridge.propose_edit(session.id, "f1.py", "edit1")
        bridge.propose_edit(session.id, "f2.py", "edit2")
        pending = bridge.get_pending_proposals()
        assert len(pending) == 2

    def test_get_pending_by_session(self, bridge: ACPIdeBridge) -> None:
        s1 = bridge.connect_ide("vscode")
        s2 = bridge.connect_ide("zed")
        bridge.propose_edit(s1.id, "f1.py", "edit1")
        bridge.propose_edit(s2.id, "f2.py", "edit2")
        pending = bridge.get_pending_proposals(session_id=s1.id)
        assert len(pending) == 1


# ---------------------------------------------------------------------------
# Tests — Generic notifications
# ---------------------------------------------------------------------------


class TestNotifications:
    def test_push_notification(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        assert bridge.push_notification(session.id, "task_complete", {"task_id": "t1"}) is True
        notifs = bridge.get_notifications(session_id=session.id)
        assert len(notifs) == 1

    def test_push_to_disconnected(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        bridge.disconnect_ide(session.id)
        assert bridge.push_notification(session.id, "test", {}) is False

    def test_get_notifications_since(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        before = time.time()
        bridge.push_notification(session.id, "t1", {})
        notifs = bridge.get_notifications(since=before - 1)
        assert len(notifs) == 1
        notifs_future = bridge.get_notifications(since=time.time() + 100)
        assert len(notifs_future) == 0


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_session_to_dict(self, session: IDESession) -> None:
        d = session.to_dict()
        assert d["editor_name"] == "jetbrains-air"
        assert d["state"] == "connected"

    def test_proposal_to_dict(self) -> None:
        p = FileEditProposal(id="p1", file_path="f.py", description="test")
        d = p.to_dict()
        assert d["id"] == "p1"
        assert d["accepted"] is None

    def test_bridge_to_dict(self, bridge: ACPIdeBridge, session: IDESession) -> None:
        d = bridge.to_dict()
        assert "sessions" in d
        assert session.id in d["sessions"]
