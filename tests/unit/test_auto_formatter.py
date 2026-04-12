"""Tests for the language-aware auto-formatter module."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.quality.auto_formatter import (
    _DEFAULT_REGISTRY,
    FormatResult,
    FormatterConfig,
    _group_files_by_formatter,
    auto_format_changed_files,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PYTHON_CFG = FormatterConfig(
    language="Python",
    command=("ruff", "format"),
    extensions=frozenset({".py"}),
    timeout_s=30,
)

_JS_CFG = FormatterConfig(
    language="JS/TS",
    command=("prettier", "--write"),
    extensions=frozenset({".js", ".ts"}),
    timeout_s=30,
)

_RUST_CFG = FormatterConfig(
    language="Rust",
    command=("rustfmt",),
    extensions=frozenset({".rs"}),
    timeout_s=30,
)

_GO_CFG = FormatterConfig(
    language="Go",
    command=("gofmt", "-w"),
    extensions=frozenset({".go"}),
    timeout_s=30,
)

_TEST_REGISTRY = (_PYTHON_CFG, _JS_CFG, _RUST_CFG, _GO_CFG)


# ---------------------------------------------------------------------------
# FormatterConfig / FormatResult dataclass tests
# ---------------------------------------------------------------------------


class TestFormatterConfig:
    """Tests for the FormatterConfig dataclass."""

    def test_frozen(self) -> None:
        with pytest.raises(AttributeError):
            _PYTHON_CFG.language = "Ruby"  # type: ignore[misc]

    def test_default_timeout(self) -> None:
        cfg = FormatterConfig(
            language="X",
            command=("x",),
            extensions=frozenset({".x"}),
        )
        assert cfg.timeout_s == 60

    def test_fields(self) -> None:
        assert _PYTHON_CFG.language == "Python"
        assert _PYTHON_CFG.command == ("ruff", "format")
        assert ".py" in _PYTHON_CFG.extensions


class TestFormatResult:
    """Tests for the FormatResult dataclass."""

    def test_frozen(self) -> None:
        r = FormatResult(
            files_formatted=1,
            files_unchanged=2,
            formatter_used="Python",
            duration_s=0.5,
        )
        with pytest.raises(AttributeError):
            r.files_formatted = 99  # type: ignore[misc]

    def test_default_error_is_none(self) -> None:
        r = FormatResult(
            files_formatted=0,
            files_unchanged=0,
            formatter_used="Python",
            duration_s=0.0,
        )
        assert r.error is None


# ---------------------------------------------------------------------------
# Default registry tests
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    """Tests for the built-in _DEFAULT_REGISTRY."""

    def test_is_tuple(self) -> None:
        assert isinstance(_DEFAULT_REGISTRY, tuple)

    def test_contains_four_languages(self) -> None:
        languages = {cfg.language for cfg in _DEFAULT_REGISTRY}
        assert languages == {"Python", "JS/TS", "Rust", "Go"}

    def test_python_uses_ruff(self) -> None:
        py = next(c for c in _DEFAULT_REGISTRY if c.language == "Python")
        assert py.command == ("ruff", "format")

    def test_go_uses_gofmt(self) -> None:
        go = next(c for c in _DEFAULT_REGISTRY if c.language == "Go")
        assert go.command == ("gofmt", "-w")


# ---------------------------------------------------------------------------
# _group_files_by_formatter tests
# ---------------------------------------------------------------------------


class TestGroupFilesByFormatter:
    """Tests for _group_files_by_formatter."""

    def test_groups_by_extension(self) -> None:
        files = ["main.py", "app.ts", "lib.rs", "server.go"]
        groups = _group_files_by_formatter(files, _TEST_REGISTRY)
        assert set(groups[_PYTHON_CFG]) == {"main.py"}
        assert set(groups[_JS_CFG]) == {"app.ts"}
        assert set(groups[_RUST_CFG]) == {"lib.rs"}
        assert set(groups[_GO_CFG]) == {"server.go"}

    def test_unknown_extensions_ignored(self) -> None:
        files = ["image.png", "data.csv", "readme.md"]
        groups = _group_files_by_formatter(files, _TEST_REGISTRY)
        assert groups == {}

    def test_first_match_wins(self) -> None:
        # Create two configs that both match .py
        cfg_a = FormatterConfig(language="A", command=("a",), extensions=frozenset({".py"}))
        cfg_b = FormatterConfig(language="B", command=("b",), extensions=frozenset({".py"}))
        groups = _group_files_by_formatter(["test.py"], (cfg_a, cfg_b))
        assert cfg_a in groups
        assert cfg_b not in groups

    def test_empty_files(self) -> None:
        groups = _group_files_by_formatter([], _TEST_REGISTRY)
        assert groups == {}

    def test_multiple_files_same_language(self) -> None:
        files = ["a.py", "b.py", "c.py"]
        groups = _group_files_by_formatter(files, _TEST_REGISTRY)
        assert len(groups[_PYTHON_CFG]) == 3


# ---------------------------------------------------------------------------
# auto_format_changed_files tests (subprocess mocked)
# ---------------------------------------------------------------------------


class TestAutoFormatChangedFiles:
    """Tests for auto_format_changed_files with mocked subprocess."""

    def test_empty_files_returns_empty(self, tmp_path: Path) -> None:
        results = auto_format_changed_files(tmp_path, [])
        assert results == []

    def test_no_matching_extensions_returns_empty(self, tmp_path: Path) -> None:
        results = auto_format_changed_files(tmp_path, ["photo.png"], registry=_TEST_REGISTRY)
        assert results == []

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_successful_python_format(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff", "format", "main.py"],
            returncode=0,
            stdout="1 file reformatted\n",
            stderr="",
        )
        results = auto_format_changed_files(tmp_path, ["main.py"], registry=(_PYTHON_CFG,))
        assert len(results) == 1
        assert results[0].files_formatted == 1
        assert results[0].formatter_used == "Python"
        assert results[0].error is None

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_no_changes_needed(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff", "format", "main.py"],
            returncode=0,
            stdout="1 file left unchanged\n",
            stderr="",
        )
        results = auto_format_changed_files(tmp_path, ["main.py"], registry=(_PYTHON_CFG,))
        assert len(results) == 1
        assert results[0].files_formatted == 0
        assert results[0].error is None

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_formatter_not_installed(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = FileNotFoundError("ruff not found")
        results = auto_format_changed_files(tmp_path, ["main.py"], registry=(_PYTHON_CFG,))
        assert len(results) == 1
        assert results[0].files_formatted == 0
        assert results[0].error is not None
        assert "not found" in results[0].error

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_formatter_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ruff", timeout=30)
        results = auto_format_changed_files(tmp_path, ["main.py"], registry=(_PYTHON_CFG,), timeout_s=30)
        assert len(results) == 1
        assert results[0].error is not None
        assert "timed out" in results[0].error

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_formatter_bad_exit_code(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff", "format", "main.py"],
            returncode=2,
            stdout="",
            stderr="internal error",
        )
        results = auto_format_changed_files(tmp_path, ["main.py"], registry=(_PYTHON_CFG,))
        assert len(results) == 1
        assert results[0].error is not None
        assert "exit code 2" in results[0].error

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_multi_language_format(self, mock_run: MagicMock, tmp_path: Path) -> None:
        def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "ruff":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="2 files reformatted\n",
                    stderr="",
                )
            if cmd[0] == "gofmt":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="",
                stderr="",
            )

        mock_run.side_effect = side_effect
        files = ["a.py", "b.py", "main.go"]
        results = auto_format_changed_files(tmp_path, files, registry=(_PYTHON_CFG, _GO_CFG))
        assert len(results) == 2
        langs = {r.formatter_used for r in results}
        assert langs == {"Python", "Go"}

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_exit_code_1_counts_as_reformatted(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Exit code 1 without 'reformatted' in stdout should still count files."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gofmt", "-w", "main.go"],
            returncode=1,
            stdout="",
            stderr="",
        )
        results = auto_format_changed_files(tmp_path, ["main.go"], registry=(_GO_CFG,))
        assert len(results) == 1
        assert results[0].files_formatted == 1
        assert results[0].error is None

    @patch("bernstein.core.quality.auto_formatter.subprocess.run")
    def test_os_error_handled_gracefully(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = OSError("Permission denied")
        results = auto_format_changed_files(tmp_path, ["main.py"], registry=(_PYTHON_CFG,))
        assert len(results) == 1
        assert results[0].error is not None

    def test_uses_default_registry_when_none(self, tmp_path: Path) -> None:
        with patch("bernstein.core.quality.auto_formatter.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            results = auto_format_changed_files(tmp_path, ["main.py"], registry=None)
            assert len(results) == 1
            # Should use default Python formatter (ruff format)
            call_args = mock_run.call_args
            assert call_args[0][0][0] == "ruff"
