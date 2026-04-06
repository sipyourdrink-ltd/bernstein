"""Tests for ENT-013: Cluster auto-scaling based on task queue depth."""

from __future__ import annotations

from bernstein.core.cluster_autoscaler import (
    AutoscaleConfig,
    ClusterAutoscaler,
    QueueSnapshot,
    ScaleDirection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    queued: int = 0,
    running: int = 0,
    nodes: int = 1,
) -> QueueSnapshot:
    return QueueSnapshot(
        total_queued=queued,
        total_running=running,
        node_count=nodes,
    )


# ---------------------------------------------------------------------------
# Scale up
# ---------------------------------------------------------------------------


class TestScaleUp:
    def test_scales_up_above_watermark(self) -> None:
        config = AutoscaleConfig(
            high_watermark=5,
            max_nodes=10,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=12, nodes=2)  # 6/node > 5
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.UP
        assert decision.recommended_nodes == 3

    def test_respects_max_nodes(self) -> None:
        config = AutoscaleConfig(
            high_watermark=1,
            max_nodes=3,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=100, nodes=3)
        decision = scaler.evaluate(snapshot)
        # Already at max, so direction should be NONE
        assert decision.direction == ScaleDirection.NONE

    def test_scale_up_step(self) -> None:
        config = AutoscaleConfig(
            high_watermark=2,
            max_nodes=20,
            scale_up_step=3,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=20, nodes=2)
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.UP
        assert decision.recommended_nodes == 5

    def test_no_nodes_scales_to_minimum(self) -> None:
        config = AutoscaleConfig(min_nodes=2)
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=5, nodes=0)
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.UP
        assert decision.recommended_nodes == 2


# ---------------------------------------------------------------------------
# Scale down
# ---------------------------------------------------------------------------


class TestScaleDown:
    def test_scales_down_below_watermark(self) -> None:
        config = AutoscaleConfig(
            low_watermark=3,
            min_nodes=1,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=2, nodes=4)  # 0.5/node < 3
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.DOWN
        assert decision.recommended_nodes == 3

    def test_respects_min_nodes(self) -> None:
        config = AutoscaleConfig(
            low_watermark=100,
            min_nodes=2,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=0, nodes=2)
        decision = scaler.evaluate(snapshot)
        # Already at min
        assert decision.direction == ScaleDirection.NONE

    def test_scale_down_step(self) -> None:
        config = AutoscaleConfig(
            low_watermark=5,
            min_nodes=1,
            scale_down_step=2,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=1, nodes=5)
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.DOWN
        assert decision.recommended_nodes == 3


# ---------------------------------------------------------------------------
# No action
# ---------------------------------------------------------------------------


class TestNoAction:
    def test_within_normal_range(self) -> None:
        config = AutoscaleConfig(
            high_watermark=10,
            low_watermark=2,
            cooldown_s=0,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=15, nodes=3)  # 5/node: between 2 and 10
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.NONE

    def test_disabled(self) -> None:
        config = AutoscaleConfig(enabled=False)
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=1000, nodes=1)
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.NONE


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_cooldown_prevents_action(self) -> None:
        config = AutoscaleConfig(
            high_watermark=1,
            cooldown_s=9999,
        )
        scaler = ClusterAutoscaler(config)

        # First evaluation triggers scale-up
        snapshot = _make_snapshot(queued=100, nodes=1)
        first = scaler.evaluate(snapshot)
        assert first.direction == ScaleDirection.UP

        # Second should be blocked by cooldown
        second = scaler.evaluate(snapshot)
        assert second.direction == ScaleDirection.NONE
        assert "cooldown" in second.reason.lower()

    def test_reset_cooldown(self) -> None:
        config = AutoscaleConfig(
            high_watermark=1,
            cooldown_s=9999,
        )
        scaler = ClusterAutoscaler(config)
        snapshot = _make_snapshot(queued=100, nodes=1)
        scaler.evaluate(snapshot)

        scaler.reset_cooldown()
        decision = scaler.evaluate(snapshot)
        assert decision.direction == ScaleDirection.UP


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    def test_records_history(self) -> None:
        config = AutoscaleConfig(cooldown_s=0)
        scaler = ClusterAutoscaler(config)
        scaler.evaluate(_make_snapshot(queued=5, nodes=2))
        scaler.evaluate(_make_snapshot(queued=5, nodes=2))
        assert len(scaler.history) == 2

    def test_clear_history(self) -> None:
        config = AutoscaleConfig(cooldown_s=0)
        scaler = ClusterAutoscaler(config)
        scaler.evaluate(_make_snapshot())
        scaler.clear_history()
        assert len(scaler.history) == 0
