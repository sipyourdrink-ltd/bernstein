"""Unit tests for `bernstein.core.knowledge.repo_analyzer`.

Covers the issue [#768](https://github.com/sipyourdrink-ltd/bernstein/issues/768)
implementation. Builds synthetic repos under `tmp_path` to exercise each
scoring branch deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.knowledge.repo_analyzer import (
    _detect_ci,
    _is_test_file,
    analyze_repo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str = "") -> Path:
    """Write *content* to root/rel, creating parents."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# is_test_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("src/foo.py", False),
        ("src/bar/quux.ts", False),
        ("tests/test_foo.py", True),
        ("tests/conftest.py", True),
        ("src/foo/__tests__/baz.test.ts", True),
        ("src/foo/bar.test.ts", True),
        ("src/foo/bar.spec.js", True),
        ("test/foo.py", True),
        ("packages/sdk/spec/api.test.ts", True),
        ("src/foo_test.py", True),
    ],
)
def test_is_test_file_matrix(rel: str, expected: bool) -> None:
    assert _is_test_file(Path(rel)) is expected


# ---------------------------------------------------------------------------
# CI detection
# ---------------------------------------------------------------------------


def test_detect_ci_github(tmp_path: Path) -> None:
    _write(tmp_path, ".github/workflows/ci.yml", "name: ci\n")
    has, kind = _detect_ci(tmp_path)
    assert has is True
    assert kind == "github"


def test_detect_ci_gitlab(tmp_path: Path) -> None:
    _write(tmp_path, ".gitlab-ci.yml", "stages: [test]\n")
    has, kind = _detect_ci(tmp_path)
    assert has is True
    assert kind == "gitlab"


def test_detect_ci_none(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# repo\n")
    has, kind = _detect_ci(tmp_path)
    assert has is False
    assert kind == ""


# ---------------------------------------------------------------------------
# Full analyze_repo passes
# ---------------------------------------------------------------------------


def test_analyze_minimal_repo(tmp_path: Path) -> None:
    _write(tmp_path, "src/foo.py", "def hello() -> str:\n    return 'hi'\n")
    a = analyze_repo(tmp_path)
    assert a.total_files == 1
    assert a.total_source_files == 1
    assert a.languages[0].name == "Python"
    assert a.test_files == 0
    assert a.has_ci is False


def test_analyze_well_structured_repo(tmp_path: Path) -> None:
    """Realistic repo with tests, CI, type hints - should score high."""
    _write(tmp_path, "src/myapp/__init__.py", "")
    _write(tmp_path, "src/myapp/api.py", "def f(x: int) -> int:\n    return x + 1\n")
    _write(tmp_path, "src/myapp/db.py", "def q(s: str) -> list:\n    return [s]\n")
    _write(tmp_path, "tests/test_api.py", "def test_f() -> None:\n    assert True\n")
    _write(tmp_path, "tests/test_db.py", "def test_q() -> None:\n    assert True\n")
    _write(tmp_path, ".github/workflows/ci.yml", "name: ci\n")
    a = analyze_repo(tmp_path)
    assert a.has_ci is True
    assert a.test_files == 2
    assert a.test_coverage_estimate_pct > 0
    # Well-structured repo should land above 6/10
    assert a.readiness_score >= 6
    assert a.files_over_300_lines == []


def test_analyze_messy_repo(tmp_path: Path) -> None:
    """Repo with one giant file, no tests, no CI - should score low."""
    big_body = "x = 1\n" * 1500
    _write(tmp_path, "src/giant.py", big_body)
    a = analyze_repo(tmp_path)
    assert a.has_ci is False
    assert a.test_files == 0
    assert a.test_coverage_estimate_pct == 0
    assert a.largest_file_lines == 1500
    assert any(p.name == "giant.py" for p, _ in a.files_over_300_lines)
    assert a.readiness_score < 4
    assert "No CI config detected" in " ".join(a.opportunities)


def test_analyze_skips_ignored_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/big-pkg/index.js", "// pretend this is huge\n" * 200)
    _write(tmp_path, "src/foo.py", "x = 1\n")
    a = analyze_repo(tmp_path)
    assert a.total_source_files == 1
    assert a.languages[0].name == "Python"


def test_analyze_multilang_repo(tmp_path: Path) -> None:
    _write(tmp_path, "src/api.ts", "export const x = 1\n")
    _write(tmp_path, "src/lib.rs", "fn main() {}\n")
    _write(tmp_path, "contracts/Escrow.sol", "// SPDX-License-Identifier: MIT\n")
    a = analyze_repo(tmp_path)
    langs = {lang.name for lang in a.languages}
    assert langs == {"TypeScript", "Rust", "Solidity"}


def test_analyze_json_serializable(tmp_path: Path) -> None:
    """Sanity check that the rich CLI's JSON output mode has no Path objects."""
    import json as _json

    _write(tmp_path, "src/foo.py", "def f() -> int: return 1\n")
    _write(tmp_path, ".github/workflows/ci.yml", "name: ci\n")
    from bernstein.cli.commands.analyze_cmd import _to_json

    a = analyze_repo(tmp_path)
    payload = _to_json(a)
    s = _json.dumps(payload)
    assert "readiness_score" in s
    assert "totals" in s


def test_analyze_min_score_exit(tmp_path: Path) -> None:
    """The CLI returns SystemExit(1) when --min-score is unmet."""
    from click.testing import CliRunner

    from bernstein.cli.commands.analyze_cmd import analyze_cmd

    _write(tmp_path, "src/giant.py", "x = 1\n" * 2000)

    runner = CliRunner()
    result = runner.invoke(
        analyze_cmd,
        ["--path", str(tmp_path), "--min-score", "6.0", "--json"],
    )
    assert result.exit_code == 1
    result = runner.invoke(
        analyze_cmd,
        ["--path", str(tmp_path), "--min-score", "0.0", "--json"],
    )
    assert result.exit_code == 0


def test_test_file_counts_ignore_checkout_ancestors(tmp_path: Path) -> None:
    """A `tests` folder above the analyzed root does not make its files tests (#8056)."""
    counts = {}
    for parent in ("examples", "tests"):
        root = tmp_path / parent / "demo"
        (root / "tests").mkdir(parents=True)
        (root / "library.py").write_text("answer = 42\n", encoding="utf-8")
        (root / "tests" / "helpers.py").write_text("x: int = 1\n", encoding="utf-8")
        (root / "test_api.py").write_text("def test_ok() -> None: pass\n", encoding="utf-8")
        counts[parent] = analyze_repo(root).test_files

    # The inner `tests/helpers.py` and `test_api.py` count under either parent; `library.py` never does.
    assert counts == {"examples": 2, "tests": 2}
