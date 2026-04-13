"""Unit tests for the agent warm pool (gh-362)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bernstein.core.warm_pool import (
    PoolSlot,
    WarmPool,
    WarmPoolConfig,
    load_warm_pool_config,
)

# ---------------------------------------------------------------------------
# PoolSlot
# ---------------------------------------------------------------------------


class TestPoolSlot:
    def test_frozen(self) -> None:
        slot = PoolSlot(
            slot_id="s1",
            role="backend",
            worktree_path="/tmp/wt1",
            created_at=1000.0,
        )
        with pytest.raises(AttributeError):
            slot.status = "claimed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        slot = PoolSlot(
            slot_id="s1",
            role="qa",
            worktree_path="/tmp/wt1",
            created_at=1000.0,
        )
        assert slot.status == "ready"
        assert slot.mcp_pid is None

    def test_fields(self) -> None:
        slot = PoolSlot(
            slot_id="s1",
            role="backend",
            worktree_path="/tmp/wt1",
            created_at=1234.5,
            status="claimed",
            mcp_pid=42,
        )
        assert slot.slot_id == "s1"
        assert slot.role == "backend"
        assert slot.worktree_path == "/tmp/wt1"
        assert slot.created_at == pytest.approx(1234.5)
        assert slot.status == "claimed"
        assert slot.mcp_pid == 42


# ---------------------------------------------------------------------------
# WarmPoolConfig
# ---------------------------------------------------------------------------


class TestWarmPoolConfig:
    def test_defaults(self) -> None:
        cfg = WarmPoolConfig()
        assert cfg.max_slots == 3
        assert cfg.slot_ttl_seconds == pytest.approx(300.0)
        assert cfg.roles == []

    def test_frozen(self) -> None:
        cfg = WarmPoolConfig()
        with pytest.raises(AttributeError):
            cfg.max_slots = 10  # type: ignore[misc]

    def test_custom_values(self) -> None:
        cfg = WarmPoolConfig(max_slots=5, slot_ttl_seconds=600.0, roles=["backend", "qa"])
        assert cfg.max_slots == 5
        assert cfg.slot_ttl_seconds == pytest.approx(600.0)
        assert cfg.roles == ["backend", "qa"]


# ---------------------------------------------------------------------------
# WarmPool -- add / claim / release / expire lifecycle
# ---------------------------------------------------------------------------


class TestWarmPoolLifecycle:
    def _make_slot(
        self,
        slot_id: str = "s1",
        role: str = "backend",
        created_at: float = 1000.0,
        status: str = "ready",
    ) -> PoolSlot:
        return PoolSlot(
            slot_id=slot_id,
            role=role,
            worktree_path=f"/tmp/{slot_id}",
            created_at=created_at,
            status=status,  # type: ignore[arg-type]
        )

    def test_add_and_stats(self) -> None:
        pool = WarmPool(WarmPoolConfig(max_slots=3))
        pool.add_slot(self._make_slot("s1"))
        pool.add_slot(self._make_slot("s2", role="qa"))
        assert pool.stats() == {"ready": 2, "claimed": 0, "expired": 0, "total": 2}

    def test_add_respects_max_slots(self) -> None:
        pool = WarmPool(WarmPoolConfig(max_slots=2))
        pool.add_slot(self._make_slot("s1"))
        pool.add_slot(self._make_slot("s2"))
        pool.add_slot(self._make_slot("s3"))  # Should be ignored
        assert pool.stats()["total"] == 2

    def test_claim_returns_matching_slot(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(self._make_slot("s1", role="backend"))
        slot = pool.claim_slot("backend")
        assert slot is not None
        assert slot.slot_id == "s1"
        assert slot.status == "claimed"

    def test_claim_returns_none_for_wrong_role(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(self._make_slot("s1", role="backend"))
        result = pool.claim_slot("qa")
        assert result is None

    def test_claim_returns_none_when_empty(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        assert pool.claim_slot("backend") is None

    def test_claim_fifo_ordering(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(self._make_slot("s1", role="backend", created_at=100.0))
        pool.add_slot(self._make_slot("s2", role="backend", created_at=200.0))
        pool.add_slot(self._make_slot("s3", role="backend", created_at=300.0))

        first = pool.claim_slot("backend")
        assert first is not None
        assert first.slot_id == "s1"

        second = pool.claim_slot("backend")
        assert second is not None
        assert second.slot_id == "s2"

        third = pool.claim_slot("backend")
        assert third is not None
        assert third.slot_id == "s3"

        assert pool.claim_slot("backend") is None

    def test_claim_skips_already_claimed(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(self._make_slot("s1", role="backend"))
        pool.add_slot(self._make_slot("s2", role="backend"))

        pool.claim_slot("backend")  # claims s1
        second = pool.claim_slot("backend")
        assert second is not None
        assert second.slot_id == "s2"

    def test_release_marks_expired(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(self._make_slot("s1", role="backend"))

        pool.claim_slot("backend")
        pool.release_slot("s1")

        st = pool.stats()
        assert st["expired"] == 1
        assert st["claimed"] == 0

    def test_release_nonexistent_is_noop(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.release_slot("nonexistent")  # Should not raise

    def test_expire_stale_marks_old_ready_slots(self) -> None:
        pool = WarmPool(WarmPoolConfig(slot_ttl_seconds=60.0))
        pool.add_slot(self._make_slot("s1", role="backend", created_at=100.0))
        pool.add_slot(self._make_slot("s2", role="backend", created_at=200.0))

        # At t=170, s1 is 70s old (> 60 TTL), s2 is 0s old
        pool.expire_stale(now=170.0)
        st = pool.stats()
        assert st["expired"] == 1  # s1
        assert st["ready"] == 1  # s2

    def test_expire_stale_does_not_touch_claimed(self) -> None:
        pool = WarmPool(WarmPoolConfig(slot_ttl_seconds=10.0))
        pool.add_slot(self._make_slot("s1", role="backend", created_at=100.0))

        pool.claim_slot("backend")
        pool.expire_stale(now=200.0)  # s1 is old but claimed

        st = pool.stats()
        assert st["claimed"] == 1
        assert st["expired"] == 0

    def test_expire_stale_uses_current_time_by_default(self) -> None:
        now = time.time()
        pool = WarmPool(WarmPoolConfig(slot_ttl_seconds=10.0))
        pool.add_slot(self._make_slot("old", role="backend", created_at=now - 100))
        pool.add_slot(self._make_slot("new", role="backend", created_at=now + 100))

        pool.expire_stale()

        st = pool.stats()
        assert st["expired"] == 1
        assert st["ready"] == 1


# ---------------------------------------------------------------------------
# WarmPool -- available_roles
# ---------------------------------------------------------------------------


class TestAvailableRoles:
    def test_empty_pool(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        assert pool.available_roles() == []

    def test_returns_ready_roles(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(PoolSlot(slot_id="s1", role="backend", worktree_path="/t/1", created_at=1.0))
        pool.add_slot(PoolSlot(slot_id="s2", role="qa", worktree_path="/t/2", created_at=2.0))
        assert pool.available_roles() == ["backend", "qa"]

    def test_excludes_claimed_and_expired(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(
            PoolSlot(
                slot_id="s1",
                role="backend",
                worktree_path="/t/1",
                created_at=1.0,
                status="claimed",
            )
        )
        pool.add_slot(
            PoolSlot(
                slot_id="s2",
                role="qa",
                worktree_path="/t/2",
                created_at=2.0,
                status="expired",
            )
        )
        assert pool.available_roles() == []

    def test_deduplicates(self) -> None:
        pool = WarmPool(WarmPoolConfig())
        pool.add_slot(PoolSlot(slot_id="s1", role="backend", worktree_path="/t/1", created_at=1.0))
        pool.add_slot(PoolSlot(slot_id="s2", role="backend", worktree_path="/t/2", created_at=2.0))
        assert pool.available_roles() == ["backend"]


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestLoadWarmPoolConfig:
    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        cfg = load_warm_pool_config(tmp_path / "nonexistent.yaml")
        assert cfg == WarmPoolConfig()

    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "warm_pool:\n  max_slots: 5\n  slot_ttl_seconds: 600\n  roles:\n    - backend\n    - qa\n",
            encoding="utf-8",
        )
        cfg = load_warm_pool_config(yaml_file)
        assert cfg.max_slots == 5
        assert cfg.slot_ttl_seconds == pytest.approx(600.0)
        assert cfg.roles == ["backend", "qa"]

    def test_missing_section_returns_defaults(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("other_section:\n  key: value\n", encoding="utf-8")
        cfg = load_warm_pool_config(yaml_file)
        assert cfg == WarmPoolConfig()

    def test_partial_config_fills_defaults(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "warm_pool:\n  max_slots: 7\n",
            encoding="utf-8",
        )
        cfg = load_warm_pool_config(yaml_file)
        assert cfg.max_slots == 7
        assert cfg.slot_ttl_seconds == pytest.approx(300.0)
        assert cfg.roles == []

    def test_invalid_yaml_returns_defaults(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(":::: not valid yaml [[", encoding="utf-8")
        cfg = load_warm_pool_config(yaml_file)
        assert cfg == WarmPoolConfig()

    def test_invalid_types_fall_back_to_defaults(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            'warm_pool:\n  max_slots: "not_a_number"\n  slot_ttl_seconds: true\n  roles: 42\n',
            encoding="utf-8",
        )
        cfg = load_warm_pool_config(yaml_file)
        assert cfg.max_slots == 3
        assert cfg.slot_ttl_seconds == pytest.approx(300.0)
        assert cfg.roles == []
