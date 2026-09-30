"""Unit tests for ``scripts/roster_terms.py``: warn-only, always exit 0."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("roster_terms_under_test", REPO_ROOT / "scripts" / "roster_terms.py")
assert _spec is not None and _spec.loader is not None
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


def test_reports_entries_inside_the_window_only() -> None:
    raw = {
        "committers": ["plain", {"login": "soon", "expires": "2026-10-15"}, {"login": "far", "expires": "2027-06-01"}],
        "area_reviewers": {"docs": [{"login": "gone", "expires": "2026-09-01"}]},
    }
    rows = rt.expiring(raw, date(2026, 9, 30), 30)
    assert [(t, login) for t, login, _ in rows] == [("area_reviewers.docs", "gone"), ("committers", "soon")]


def test_main_exits_zero_even_with_expired_entries(tmp_path: Path, capsys) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "quorum-roster.toml").write_text(
        'maintainer = "o"\ncommitters = [{ login = "x", expires = "2020-01-01" }]\n', encoding="utf-8"
    )
    assert rt.main(["--root", str(tmp_path)]) == 0
    assert "x (committers)" in capsys.readouterr().out
