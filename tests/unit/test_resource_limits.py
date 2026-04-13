"""Tests for agent process resource limits (AGENT-013)."""

from __future__ import annotations

import os

import pytest
from bernstein.core.resource_limits import (
    EnforcementResult,
    ResourceLimits,
    ResourceUsage,
    apply_limits,
    check_usage,
)


class TestResourceLimits:
    def test_defaults_are_unlimited(self) -> None:
        limits = ResourceLimits()
        assert limits.memory_mb == 0
        assert limits.cpu_seconds == 0
        assert limits.open_files == 0
        assert limits.disk_write_mb == 0

    def test_custom_values(self) -> None:
        limits = ResourceLimits(memory_mb=2048, cpu_seconds=600, open_files=1024)
        assert limits.memory_mb == 2048
        assert limits.cpu_seconds == 600
        assert limits.open_files == 1024


class TestEnforcementResult:
    def test_default_state(self) -> None:
        result = EnforcementResult()
        assert not result.applied
        assert result.advisory_only
        assert result.warnings == []

    def test_warnings_initialized(self) -> None:
        result = EnforcementResult()
        result.warnings.append("test")
        assert len(result.warnings) == 1


class TestResourceUsage:
    def test_default_state(self) -> None:
        usage = ResourceUsage()
        assert usage.rss_mb == 0.0
        assert not usage.memory_exceeded
        assert not usage.cpu_exceeded


class TestApplyLimits:
    def test_no_op_with_zero_limits(self) -> None:
        limits = ResourceLimits()
        result = apply_limits(limits)
        # On POSIX should apply (even if nothing to set), on non-POSIX advisory
        assert isinstance(result, EnforcementResult)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX only")
    def test_applies_on_posix(self) -> None:
        limits = ResourceLimits(open_files=4096)
        result = apply_limits(limits)
        assert result.applied
        # open_files enforcement may or may not succeed depending on system limits
        assert isinstance(result.open_files_enforced, bool)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX only")
    def test_cpu_limit_applied(self) -> None:
        limits = ResourceLimits(cpu_seconds=99999)
        result = apply_limits(limits)
        assert result.applied
        assert result.cpu_enforced


class TestCheckUsage:
    def test_check_self_process(self) -> None:
        limits = ResourceLimits(memory_mb=999999, cpu_seconds=999999)
        usage = check_usage(os.getpid(), limits)
        assert isinstance(usage, ResourceUsage)
        # Should not exceed enormous limits
        assert not usage.memory_exceeded
        assert not usage.cpu_exceeded

    def test_check_nonexistent_pid(self) -> None:
        limits = ResourceLimits(memory_mb=100)
        usage = check_usage(9999999, limits)
        # Should not crash, just return defaults
        assert isinstance(usage, ResourceUsage)

    def test_memory_exceeded_flag(self) -> None:
        usage = ResourceUsage(rss_mb=500.0, memory_exceeded=True)
        assert usage.memory_exceeded
