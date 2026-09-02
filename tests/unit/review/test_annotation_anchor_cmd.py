"""CLI tests for ``bernstein review-annotation derive|resolve`` (#3456).

The surface is read-only: it derives an anchor from a file on disk and
resolves an existing anchor against the file's current bytes. It writes
nothing, so an operator can inspect an annotation's fate without mutating a
run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from bernstein.cli.commands.review_annotation_cmd import review_annotation_group

_BASE = "alpha\nbravo\nTARGET-1\nTARGET-2\nTARGET-3\ncharlie\n"


def _derive(runner: CliRunner, target: Path) -> dict[str, Any]:
    result = runner.invoke(
        review_annotation_group,
        ["derive", "--file", str(target), "--start-line", "3", "--end-line", "5", "--comment", "rename this"],
    )
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.output)
    return payload


def test_derive_prints_the_canonical_anchor_and_leaves_the_file_untouched(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text(_BASE, encoding="utf-8")
    runner = CliRunner()

    payload = _derive(runner, target)

    assert payload["blob_sha256"].startswith("sha256:")
    assert payload["start_line"] == 3
    assert payload["end_line"] == 5
    assert target.read_text(encoding="utf-8") == _BASE


def test_resolve_reports_the_shifted_range_after_an_insertion(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text(_BASE, encoding="utf-8")
    runner = CliRunner()
    payload = _derive(runner, target)
    anchor_file = tmp_path / "anchor.json"
    anchor_file.write_text(json.dumps(payload), encoding="utf-8")
    target.write_text("pad\npad\n" + _BASE, encoding="utf-8")

    result = runner.invoke(review_annotation_group, ["resolve", "--anchor", str(anchor_file), "--file", str(target)])

    assert result.exit_code == 0, result.output
    resolution: dict[str, Any] = json.loads(result.output)
    assert resolution["status"] == "resolved"
    assert resolution["start_line"] == 5
    assert resolution["end_line"] == 7


def test_resolve_exits_nonzero_and_says_orphaned_when_the_target_bytes_are_gone(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text(_BASE, encoding="utf-8")
    runner = CliRunner()
    payload = _derive(runner, target)
    anchor_file = tmp_path / "anchor.json"
    anchor_file.write_text(json.dumps(payload), encoding="utf-8")
    target.write_text("alpha\nbravo\nusurper\nusurper\nusurper\ncharlie\n", encoding="utf-8")

    result = runner.invoke(review_annotation_group, ["resolve", "--anchor", str(anchor_file), "--file", str(target)])

    assert result.exit_code == 1
    resolution: dict[str, Any] = json.loads(result.output)
    assert resolution["status"] == "orphaned"
    assert resolution["reason"] == "target_bytes_absent"
    assert resolution["start_line"] is None
