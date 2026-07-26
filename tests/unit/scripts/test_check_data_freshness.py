"""A freshness inventory entry whose file is missing must be reported.

``scripts/check_data_freshness.py`` walks a hand-maintained ``INVENTORY`` of
docs carrying ``as of YYYY-MM-DD`` markers. ``_scan_file`` returned an empty
result for a path that does not exist, so a renamed or deleted doc quietly
stopped being checked while the script kept printing that every marker was
fresh. Two entries had been inert for that reason before anyone noticed.

A missing entry is either a stale list entry to remove or a file that went
missing; both need a human, so the script reports it and fails rather than
skipping it.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_data_freshness.py"


@pytest.fixture
def freshness_module() -> Generator[ModuleType, None, None]:
    """Load scripts/check_data_freshness.py as an importable module."""
    spec = importlib.util.spec_from_file_location("check_data_freshness_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_missing_inventory_entries_are_listed(freshness_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """``missing_inventory_entries`` names every entry with no file on disk."""
    monkeypatch.setattr(
        freshness_module,
        "INVENTORY",
        ("README.md", "docs/deleted-in-a-rename.md"),
    )
    assert freshness_module.missing_inventory_entries() == ["docs/deleted-in-a-rename.md"]


def test_shipped_inventory_has_no_missing_entries(freshness_module: ModuleType) -> None:
    """Every entry the repo ships today points at a file that exists."""
    assert freshness_module.missing_inventory_entries() == []


def test_missing_entry_fails_the_check(
    freshness_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing entry exits non-zero and names the path, without --strict.

    Staleness is temporal and resolves itself; a missing inventory entry does
    not, so it is reported the same way on every invocation.
    """
    monkeypatch.setattr(
        freshness_module,
        "INVENTORY",
        ("README.md", "docs/deleted-in-a-rename.md"),
    )

    rc = freshness_module.main(["--today", "2026-01-01"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 1
    assert "docs/deleted-in-a-rename.md" in combined
    assert "missing" in combined.lower()


def test_intact_inventory_still_reports_fresh(
    freshness_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With every entry present and fresh the check stays green."""
    doc = tmp_path / "fresh.md"
    doc.write_text("Downloads as of 2026-01-01: 12345\n", encoding="utf-8")
    monkeypatch.setattr(freshness_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(freshness_module, "INVENTORY", ("fresh.md",))

    rc = freshness_module.main(["--today", "2026-01-10"])

    assert rc == 0
    assert "all markers under" in capsys.readouterr().out
