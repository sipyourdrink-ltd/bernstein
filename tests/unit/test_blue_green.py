"""Tests for blue-green deployment support."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.blue_green import (
    BlueGreenConfig,
    BlueGreenDeployment,
    DeploymentStatus,
)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Return a tmp project root with a real .sdd/ directory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "config").mkdir()
    (sdd / "config" / "settings.json").write_text("{}")
    return tmp_path


@pytest.fixture
def config() -> BlueGreenConfig:
    """Return a default config with no delay for fast tests."""
    return BlueGreenConfig(switch_delay_seconds=0)


# ------------------------------------------------------------------
# prepare_green
# ------------------------------------------------------------------


class TestPrepareGreen:
    """Tests for BlueGreenDeployment.prepare_green."""

    def test_creates_green_directory(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        green = deploy.prepare_green("1.0.0")

        assert green.exists()
        assert green == base / ".sdd-green"

    def test_copies_config_from_blue(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")

        assert (base / ".sdd-green" / "config" / "settings.json").exists()

    def test_writes_version_file(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("2.5.0")

        vfile = base / ".sdd-green" / "version.json"
        assert vfile.exists()
        data = json.loads(vfile.read_text())
        assert data["version"] == "2.5.0"

    def test_replaces_existing_green(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")
        deploy.prepare_green("2.0.0")

        data = json.loads((base / ".sdd-green" / "version.json").read_text())
        assert data["version"] == "2.0.0"

    def test_bootstraps_blue_from_sdd(self, base: Path, config: BlueGreenConfig) -> None:
        """First prepare_green should promote .sdd/ to .sdd-blue/."""
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")

        assert (base / ".sdd-blue").is_dir()
        assert (base / ".sdd").is_symlink()


# ------------------------------------------------------------------
# switch_traffic
# ------------------------------------------------------------------


class TestSwitchTraffic:
    """Tests for BlueGreenDeployment.switch_traffic."""

    def test_swaps_active_env(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")
        deploy.switch_traffic()

        assert (base / ".sdd").is_symlink()
        assert (base / ".sdd").resolve() == (base / ".sdd-green").resolve()

    def test_raises_when_green_missing(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy._ensure_blue()

        with pytest.raises(FileNotFoundError):
            deploy.switch_traffic()


# ------------------------------------------------------------------
# rollback
# ------------------------------------------------------------------


class TestRollback:
    """Tests for BlueGreenDeployment.rollback."""

    def test_restores_blue(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")
        deploy.switch_traffic()
        deploy.rollback()

        assert (base / ".sdd").is_symlink()
        assert (base / ".sdd").resolve() == (base / ".sdd-blue").resolve()


# ------------------------------------------------------------------
# health_check
# ------------------------------------------------------------------


class TestHealthCheck:
    """Tests for BlueGreenDeployment.health_check."""

    def test_returns_true_when_green_has_version(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")

        assert deploy.health_check() is True

    def test_returns_false_when_green_missing(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)

        assert deploy.health_check() is False

    def test_http_health_check_success(self, base: Path) -> None:
        cfg = BlueGreenConfig(health_check_url="http://127.0.0.1:8052/status")
        deploy = BlueGreenDeployment(cfg, base)

        with patch("bernstein.core.orchestration.blue_green.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            assert deploy.health_check() is True

    def test_http_health_check_failure(self, base: Path) -> None:
        cfg = BlueGreenConfig(health_check_url="http://127.0.0.1:8052/status")
        deploy = BlueGreenDeployment(cfg, base)

        with patch("bernstein.core.orchestration.blue_green.httpx.get") as mock_get:
            mock_get.return_value.status_code = 503
            assert deploy.health_check() is False


# ------------------------------------------------------------------
# status
# ------------------------------------------------------------------


class TestStatus:
    """Tests for BlueGreenDeployment.status."""

    def test_reports_blue_active_initially(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")

        st = deploy.status()

        assert isinstance(st, DeploymentStatus)
        assert st.active == "blue"
        assert st.green_version == "1.0.0"

    def test_reports_green_active_after_switch(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")
        deploy.switch_traffic()

        st = deploy.status()

        assert st.active == "green"
        assert st.healthy is True

    def test_reports_blue_after_rollback(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy.prepare_green("1.0.0")
        deploy.switch_traffic()
        deploy.rollback()

        st = deploy.status()

        assert st.active == "blue"

    def test_no_green_version_when_not_prepared(self, base: Path, config: BlueGreenConfig) -> None:
        deploy = BlueGreenDeployment(config, base)
        deploy._ensure_blue()

        st = deploy.status()

        assert st.green_version is None


# ------------------------------------------------------------------
# BlueGreenConfig defaults
# ------------------------------------------------------------------


class TestBlueGreenConfig:
    """Tests for BlueGreenConfig dataclass defaults."""

    def test_defaults(self) -> None:
        cfg = BlueGreenConfig()
        assert cfg.strategy == "rolling"
        assert cfg.health_check_url == ""
        assert cfg.rollback_on_error is True
        assert cfg.switch_delay_seconds == 10

    def test_custom_values(self) -> None:
        cfg = BlueGreenConfig(
            strategy="canary",
            health_check_url="http://localhost/health",
            rollback_on_error=False,
            switch_delay_seconds=30,
        )
        assert cfg.strategy == "canary"
        assert cfg.rollback_on_error is False


# ------------------------------------------------------------------
# DeploymentStatus
# ------------------------------------------------------------------


class TestDeploymentStatus:
    """Tests for DeploymentStatus dataclass."""

    def test_fields(self) -> None:
        st = DeploymentStatus(
            active="green",
            blue_version="1.0.0",
            green_version="2.0.0",
            healthy=True,
        )
        assert st.active == "green"
        assert st.blue_version == "1.0.0"
        assert st.green_version == "2.0.0"
        assert st.healthy is True
