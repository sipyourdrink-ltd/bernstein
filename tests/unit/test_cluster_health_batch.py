"""Tests for health evaluation and cluster seed thresholds."""

from __future__ import annotations

import pytest

from bernstein.core.config.seed_parser import SeedError, _parse_cluster
from bernstein.core.protocols.cluster.cluster import (
    HealthThresholds,
    health_verdict,
)


class TestHealthVerdict:
    def test_all_ok(self) -> None:
        assert health_verdict(disk_free_mb=2048, mem_used_pct=50.0, mesh_rtt_ms=10.0) == "ok"

    def test_low_disk(self) -> None:
        v = health_verdict(disk_free_mb=100)
        assert v == "unhealthy:disk"

    def test_high_memory(self) -> None:
        v = health_verdict(mem_used_pct=98.0)
        assert v == "unhealthy:memory"

    def test_high_rtt(self) -> None:
        v = health_verdict(mesh_rtt_ms=10000.0)
        assert v == "unhealthy:mesh-rtt"

    def test_multiple_reasons(self) -> None:
        v = health_verdict(disk_free_mb=10, mem_used_pct=99.0, mesh_rtt_ms=60000.0)
        assert "disk" in v
        assert "memory" in v
        assert "mesh-rtt" in v

    def test_none_values_skipped(self) -> None:
        assert health_verdict() == "ok"

    def test_custom_thresholds(self) -> None:
        t = HealthThresholds(min_disk_free_mb=2048)
        assert health_verdict(disk_free_mb=1024, thresholds=t) == "unhealthy:disk"
        assert health_verdict(disk_free_mb=4096, thresholds=t) == "ok"


def test_health_thresholds_parse_from_seed() -> None:
    config = _parse_cluster({"min_disk_free_mb": 2048, "max_mem_used_pct": 80, "max_mesh_rtt_ms": 200})
    assert config is not None
    assert (config.min_disk_free_mb, config.max_mem_used_pct, config.max_mesh_rtt_ms) == (2048, 80.0, 200.0)


@pytest.mark.parametrize(
    "key,value",
    [
        ("min_disk_free_mb", -1),
        ("min_disk_free_mb", True),
        ("max_mem_used_pct", 101),
        ("max_mesh_rtt_ms", float("nan")),
        ("max_mem_used_pct", "bad"),
        ("max_mesh_rtt_ms", float("inf")),
    ],
)
def test_invalid_health_thresholds_are_rejected(key: str, value: object) -> None:
    with pytest.raises(SeedError, match="cluster"):
        _parse_cluster({key: value})
