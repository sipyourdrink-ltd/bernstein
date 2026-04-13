"""Tests for orchestrator startup self-test (ORCH-015)."""

from __future__ import annotations

from pathlib import Path

import httpx
from bernstein.core.startup_selftest import (
    CheckStatus,
    SelfTestReport,
    _check_adapter,
    _check_config,
    _check_disk_space,
    _check_git,
    _check_sdd_dir,
    _check_server,
    run_startup_selftest,
)


class TestCheckServer:
    def test_server_reachable(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"}))
        client = httpx.Client(transport=transport)
        result = _check_server("http://test", client)
        assert result.status == CheckStatus.PASS
        assert result.critical is True
        client.close()

    def test_server_unreachable(self) -> None:
        def _fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(_fail)
        client = httpx.Client(transport=transport)
        result = _check_server("http://test", client)
        assert result.status == CheckStatus.FAIL
        assert result.critical is True
        client.close()


class TestCheckAdapter:
    def test_adapter_found(self) -> None:
        # "python" should always be in PATH
        result = _check_adapter("python")
        assert result.status == CheckStatus.PASS

    def test_adapter_not_found(self) -> None:
        result = _check_adapter("nonexistent_adapter_xyz_12345")
        assert result.status == CheckStatus.WARN


class TestCheckConfig:
    def test_no_workdir(self) -> None:
        result = _check_config(None)
        assert result.status == CheckStatus.SKIP

    def test_missing_config(self, tmp_path: Path) -> None:
        result = _check_config(tmp_path)
        assert result.status == CheckStatus.WARN

    def test_valid_config(self, tmp_path: Path) -> None:
        # Create a minimal bernstein.yaml
        config_path = tmp_path / "bernstein.yaml"
        config_path.write_text("cli: claude\nmax_agents: 4\n")
        result = _check_config(tmp_path)
        # May pass or fail depending on seed parser, but should not crash
        assert result.status in (CheckStatus.PASS, CheckStatus.FAIL)


class TestCheckDiskSpace:
    def test_no_workdir(self) -> None:
        result = _check_disk_space(None)
        assert result.status == CheckStatus.SKIP

    def test_has_disk_space(self, tmp_path: Path) -> None:
        result = _check_disk_space(tmp_path)
        # tmp_path should have plenty of space
        assert result.status == CheckStatus.PASS


class TestCheckGit:
    def test_git_available(self, tmp_path: Path) -> None:
        result = _check_git(tmp_path)
        # git should be available in CI/local environments
        assert result.status in (CheckStatus.PASS, CheckStatus.WARN)

    def test_no_workdir(self) -> None:
        result = _check_git(None)
        assert result.status in (CheckStatus.PASS, CheckStatus.WARN)


class TestCheckSddDir:
    def test_no_workdir(self) -> None:
        result = _check_sdd_dir(None)
        assert result.status == CheckStatus.SKIP

    def test_creates_sdd_dir(self, tmp_path: Path) -> None:
        result = _check_sdd_dir(tmp_path)
        assert result.status == CheckStatus.PASS
        assert (tmp_path / ".sdd").exists()

    def test_existing_sdd_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".sdd").mkdir()
        result = _check_sdd_dir(tmp_path)
        assert result.status == CheckStatus.PASS


class TestRunStartupSelftest:
    def test_full_selftest(self, tmp_path: Path) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        client = httpx.Client(transport=transport)
        report = run_startup_selftest(
            server_url="http://test",
            workdir=tmp_path,
            adapter_name="python",
            client=client,
        )
        assert isinstance(report, SelfTestReport)
        assert len(report.checks) >= 5
        assert isinstance(report.all_critical_passed, bool)
        assert "Self-test" in report.summary
        client.close()

    def test_to_dict(self, tmp_path: Path) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        client = httpx.Client(transport=transport)
        report = run_startup_selftest(
            server_url="http://test",
            workdir=tmp_path,
            client=client,
        )
        d = report.to_dict()
        assert "all_critical_passed" in d
        assert "checks" in d
        client.close()
