"""Tests for file-backed team state tracking."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.team_state import TeamMember, TeamStateStore

# ---------------------------------------------------------------------------
# TeamMember dataclass
# ---------------------------------------------------------------------------


class TestTeamMember:
    def test_defaults(self) -> None:
        m = TeamMember(agent_id="a1", role="backend")
        assert m.agent_id == "a1"
        assert m.role == "backend"
        assert m.model == ""
        assert m.status == "starting"
        assert m.is_active is True
        assert m.task_ids == []
        assert m.spawned_at == 0.0
        assert m.finished_at == 0.0
        assert m.provider == ""

    def test_to_dict_round_trip(self) -> None:
        m = TeamMember(
            agent_id="b2",
            role="qa",
            model="opus",
            status="working",
            is_active=True,
            task_ids=["T1", "T2"],
            spawned_at=100.0,
            provider="claude",
        )
        d = m.to_dict()
        assert d["agent_id"] == "b2"
        assert d["model"] == "opus"
        assert d["task_ids"] == ["T1", "T2"]

        restored = TeamMember.from_dict(d)
        assert restored.agent_id == m.agent_id
        assert restored.role == m.role
        assert restored.model == m.model
        assert restored.status == m.status
        assert restored.is_active == m.is_active
        assert restored.task_ids == m.task_ids
        assert restored.spawned_at == m.spawned_at
        assert restored.provider == m.provider

    def test_from_dict_missing_keys(self) -> None:
        m = TeamMember.from_dict({})
        assert m.agent_id == ""
        assert m.role == ""
        assert m.status == "starting"
        assert m.is_active is True


# ---------------------------------------------------------------------------
# TeamStateStore — persistence
# ---------------------------------------------------------------------------


class TestTeamStateStore:
    def test_empty_store_returns_no_members(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        assert store.list_members() == []
        assert store.active_count() == 0

    def test_on_spawn_registers_member(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        member = store.on_spawn("agent-1", "backend", model="sonnet", task_ids=["T1"])

        assert member.agent_id == "agent-1"
        assert member.role == "backend"
        assert member.model == "sonnet"
        assert member.status == "starting"
        assert member.is_active is True
        assert member.task_ids == ["T1"]
        assert member.spawned_at > 0

    def test_on_spawn_persists_to_disk(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")

        # Read file directly
        team_file = tmp_path / "runtime" / "team.json"
        assert team_file.exists()
        data = json.loads(team_file.read_text(encoding="utf-8"))
        assert len(data["members"]) == 1
        assert data["members"][0]["agent_id"] == "agent-1"
        assert "updated_at" in data

    def test_multiple_spawns(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend", model="sonnet")
        store.on_spawn("agent-2", "qa", model="haiku")
        store.on_spawn("agent-3", "security", model="opus")

        members = store.list_members()
        assert len(members) == 3

        ids = {m.agent_id for m in members}
        assert ids == {"agent-1", "agent-2", "agent-3"}

    def test_on_status_change(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")

        updated = store.on_status_change("agent-1", "working")
        assert updated is not None
        assert updated.status == "working"
        assert updated.is_active is True

        # Read back from a fresh store instance
        store2 = TeamStateStore(tmp_path)
        member = store2.get_member("agent-1")
        assert member is not None
        assert member.status == "working"

    def test_on_status_change_unknown_agent(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        result = store.on_status_change("nonexistent", "working")
        assert result is None

    def test_on_complete_marks_dead(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")

        result = store.on_complete("agent-1")
        assert result is not None
        assert result.status == "dead"
        assert result.is_active is False
        assert result.finished_at > 0

    def test_on_fail_marks_dead(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")

        result = store.on_fail("agent-1")
        assert result is not None
        assert result.status == "dead"
        assert result.is_active is False

    def test_on_kill_marks_dead(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")

        result = store.on_kill("agent-1")
        assert result is not None
        assert result.status == "dead"
        assert result.is_active is False

    def test_list_members_active_only(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")
        store.on_spawn("agent-2", "qa")
        store.on_complete("agent-1")

        all_members = store.list_members()
        assert len(all_members) == 2

        active = store.list_members(active_only=True)
        assert len(active) == 1
        assert active[0].agent_id == "agent-2"

    def test_active_count(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")
        store.on_spawn("agent-2", "qa")

        assert store.active_count() == 2

        store.on_complete("agent-1")
        assert store.active_count() == 1

    def test_get_member_returns_none_when_absent(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        assert store.get_member("not-here") is None

    def test_get_member_returns_member(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend", model="opus", provider="claude")

        member = store.get_member("agent-1")
        assert member is not None
        assert member.model == "opus"
        assert member.provider == "claude"

    def test_summary(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend", model="sonnet")
        store.on_spawn("agent-2", "qa", model="haiku")
        store.on_spawn("agent-3", "backend", model="sonnet")
        store.on_complete("agent-2")

        summary = store.summary()
        assert summary["total_members"] == 3
        assert summary["active_count"] == 2
        assert summary["finished_count"] == 1
        assert summary["roles"] == {"backend": 2}
        assert len(summary["members"]) == 3

    def test_clear_removes_state(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")
        assert store.active_count() == 1

        store.clear()
        assert store.list_members() == []
        assert store.active_count() == 0

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        team_file = tmp_path / "runtime" / "team.json"
        team_file.write_text("not valid json{{{", encoding="utf-8")

        members = store.list_members()
        assert members == []

    def test_cross_process_visibility(self, tmp_path: Path) -> None:
        """Two store instances backed by the same directory see each other's writes."""
        store_a = TeamStateStore(tmp_path)
        store_b = TeamStateStore(tmp_path)

        store_a.on_spawn("agent-1", "backend")
        members = store_b.list_members()
        assert len(members) == 1
        assert members[0].agent_id == "agent-1"

        store_b.on_complete("agent-1")
        member = store_a.get_member("agent-1")
        assert member is not None
        assert member.is_active is False

    def test_finished_at_not_overwritten_on_second_dead_call(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        store.on_spawn("agent-1", "backend")
        result1 = store.on_complete("agent-1")
        assert result1 is not None
        first_ts = result1.finished_at

        # Calling on_kill again should not overwrite finished_at
        result2 = store.on_kill("agent-1")
        assert result2 is not None
        assert result2.finished_at == first_ts

    def test_on_spawn_with_provider(self, tmp_path: Path) -> None:
        store = TeamStateStore(tmp_path)
        member = store.on_spawn("agent-1", "backend", provider="codex")
        assert member.provider == "codex"

    def test_summary_roles_only_count_active(self, tmp_path: Path) -> None:
        """The roles dict in the summary only counts active members."""
        store = TeamStateStore(tmp_path)
        store.on_spawn("a1", "backend")
        store.on_spawn("a2", "backend")
        store.on_complete("a2")

        summary = store.summary()
        assert summary["roles"] == {"backend": 1}
