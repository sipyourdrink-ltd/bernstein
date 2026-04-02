"""Tests for Chaos Engineering CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.chaos_cmd import chaos_group


def test_chaos_rate_limit(tmp_path: Path) -> None:
    """Test the chaos rate-limit command."""
    # We need to mock CHAOS_DIR to use tmp_path
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["rate-limit", "--duration", "10", "--provider", "test-p"])

        assert result.exit_code == 0
        assert "Provider test-p rate-limited" in result.output

        # Verify file creation
        rate_limit_file = tmp_path / "rate_limit_active.json"
        assert rate_limit_file.exists()
        data = json.loads(rate_limit_file.read_text())
        assert data["provider"] == "test-p"


def test_chaos_status_empty(tmp_path: Path) -> None:
    """Test chaos status when no events recorded."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["status"])
        assert result.exit_code == 0
        assert "No chaos experiments recorded yet" in result.output
