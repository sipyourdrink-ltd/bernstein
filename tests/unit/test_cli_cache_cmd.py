"""Tests for `bernstein cache` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.semantic_cache import ResponseCacheManager


def _seed_cache(workdir: Path) -> None:
    mgr = ResponseCacheManager(workdir)
    mgr.store(
        ResponseCacheManager.task_key("backend", "Verified task", "desc"),
        "verified result",
        verified=True,
        git_diff_lines=11,
        source_task_id="T-verified",
    )
    mgr.store(
        ResponseCacheManager.task_key("backend", "Ghost task", "desc"),
        "ghost result",
        verified=False,
        source_task_id="T-ghost",
    )
    mgr.save()


def test_cache_list_json(tmp_path: Path) -> None:
    _seed_cache(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["cache", "list", "--json", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload["entries"]) == 2
    assert payload["entries"][0]["source_task_id"] in {"T-verified", "T-ghost"}


def test_cache_inspect_json(tmp_path: Path) -> None:
    _seed_cache(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["cache", "inspect", "T-verified", "--json", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["source_task_id"] == "T-verified"
    assert payload["verified"] is True


def test_cache_inspect_missing_task_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["cache", "inspect", "missing", "--workdir", str(tmp_path)])

    assert result.exit_code == 1
    assert "No cache entry found" in result.output


def test_cache_clear_unverified_only(tmp_path: Path) -> None:
    _seed_cache(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["cache", "clear", "--unverified", "--yes", "--workdir", str(tmp_path)])

    assert result.exit_code == 0
    mgr = ResponseCacheManager(tmp_path)
    assert mgr.inspect_task("T-verified") is not None
    assert mgr.inspect_task("T-ghost") is None
