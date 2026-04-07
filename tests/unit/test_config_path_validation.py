"""Tests for bernstein.core.config_path_validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.config_path_validation import (
    PathValidationError,
    PathValidationResult,
    check_config_paths,
    validate_config_paths,
)
from bernstein.core.seed import SeedConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """Return a temporary working directory."""
    return tmp_path


# ---------------------------------------------------------------------------
# PathValidationError tests
# ---------------------------------------------------------------------------


class TestPathValidationError:
    """Tests for PathValidationError dataclass."""

    def test_str_format(self) -> None:
        err = PathValidationError(
            field="context_files",
            path="docs/DESIGN.md",
            reason="does not exist",
        )
        assert str(err) == "context_files: 'docs/DESIGN.md' does not exist"

    def test_frozen(self) -> None:
        err = PathValidationError(field="f", path="p", reason="r")
        with pytest.raises(AttributeError):
            err.field = "new"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PathValidationResult tests
# ---------------------------------------------------------------------------


class TestPathValidationResult:
    """Tests for PathValidationResult dataclass."""

    def test_ok_when_no_errors(self) -> None:
        result = PathValidationResult(errors=())
        assert result.ok is True

    def test_not_ok_when_errors(self) -> None:
        err = PathValidationError(field="f", path="p", reason="r")
        result = PathValidationResult(errors=(err,))
        assert result.ok is False

    def test_format_errors_empty(self) -> None:
        result = PathValidationResult(errors=())
        assert result.format_errors() == ""

    def test_format_errors_multiple(self) -> None:
        e1 = PathValidationError(field="f1", path="p1", reason="r1")
        e2 = PathValidationError(field="f2", path="p2", reason="r2")
        result = PathValidationResult(errors=(e1, e2))
        formatted = result.format_errors()
        assert "  - f1: 'p1' r1" in formatted
        assert "  - f2: 'p2' r2" in formatted


# ---------------------------------------------------------------------------
# validate_config_paths tests — context_files
# ---------------------------------------------------------------------------


class TestValidateContextFiles:
    """Tests for context_files validation."""

    def test_valid_context_file(self, workdir: Path) -> None:
        """Existing file passes validation."""
        (workdir / "docs").mkdir()
        (workdir / "docs" / "DESIGN.md").write_text("# Design")
        cfg = SeedConfig(goal="Test", context_files=("docs/DESIGN.md",))
        result = validate_config_paths(cfg, workdir)
        assert result.ok

    def test_missing_context_file(self, workdir: Path) -> None:
        """Missing file fails with clear error message."""
        cfg = SeedConfig(goal="Test", context_files=("docs/DESIGN.md",))
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.field == "context_files"
        assert err.path == "docs/DESIGN.md"
        assert err.reason == "does not exist"

    def test_context_file_is_directory(self, workdir: Path) -> None:
        """Directory instead of file fails validation."""
        (workdir / "docs").mkdir()
        cfg = SeedConfig(goal="Test", context_files=("docs",))
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert result.errors[0].reason == "is not a file"

    def test_multiple_context_files_partial_invalid(self, workdir: Path) -> None:
        """Multiple files: only missing ones are reported."""
        (workdir / "exists.md").write_text("ok")
        cfg = SeedConfig(goal="Test", context_files=("exists.md", "missing.md"))
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert len(result.errors) == 1
        assert result.errors[0].path == "missing.md"

    def test_multiple_context_files_all_missing(self, workdir: Path) -> None:
        """All missing files are reported."""
        cfg = SeedConfig(goal="Test", context_files=("a.md", "b.md"))
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert len(result.errors) == 2

    def test_empty_context_files(self, workdir: Path) -> None:
        """Empty context_files passes validation."""
        cfg = SeedConfig(goal="Test", context_files=())
        result = validate_config_paths(cfg, workdir)
        assert result.ok

    def test_valid_context_file_absolute_path(self, workdir: Path) -> None:
        """Absolute path to existing file passes validation."""
        context_file = workdir / "absolute_spec.md"
        context_file.write_text("# Spec")
        cfg = SeedConfig(goal="Test", context_files=(str(context_file),))
        result = validate_config_paths(cfg, workdir)
        assert result.ok

    def test_missing_context_file_absolute_path(self, workdir: Path) -> None:
        """Missing absolute path fails with clear error."""
        missing_path = workdir / "nonexistent" / "file.md"
        cfg = SeedConfig(goal="Test", context_files=(str(missing_path),))
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert len(result.errors) == 1
        assert result.errors[0].field == "context_files"
        assert result.errors[0].reason == "does not exist"


# ---------------------------------------------------------------------------
# validate_config_paths tests — agent_catalog
# ---------------------------------------------------------------------------


class TestValidateAgentCatalog:
    """Tests for agent_catalog validation."""

    def test_valid_agent_catalog_relative(self, workdir: Path) -> None:
        """Existing directory passes validation."""
        (workdir / "agents").mkdir()
        cfg = SeedConfig(goal="Test", agent_catalog="agents")
        result = validate_config_paths(cfg, workdir)
        assert result.ok

    def test_valid_agent_catalog_absolute(self, workdir: Path) -> None:
        """Absolute path to existing directory passes."""
        agents_dir = workdir / "agents"
        agents_dir.mkdir()
        cfg = SeedConfig(goal="Test", agent_catalog=str(agents_dir))
        result = validate_config_paths(cfg, workdir)
        assert result.ok

    def test_missing_agent_catalog(self, workdir: Path) -> None:
        """Missing directory fails with clear error."""
        cfg = SeedConfig(goal="Test", agent_catalog="/nonexistent/path")
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert len(result.errors) == 1
        err = result.errors[0]
        assert err.field == "agent_catalog"
        assert err.reason == "does not exist"

    def test_agent_catalog_is_file(self, workdir: Path) -> None:
        """File instead of directory fails validation."""
        (workdir / "agents.txt").write_text("not a dir")
        cfg = SeedConfig(goal="Test", agent_catalog="agents.txt")
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert result.errors[0].reason == "is not a directory"

    def test_agent_catalog_none(self, workdir: Path) -> None:
        """None agent_catalog skips validation."""
        cfg = SeedConfig(goal="Test", agent_catalog=None)
        result = validate_config_paths(cfg, workdir)
        assert result.ok


# ---------------------------------------------------------------------------
# validate_config_paths tests — combined
# ---------------------------------------------------------------------------


class TestValidateCombined:
    """Tests for combined validation scenarios."""

    def test_multiple_errors(self, workdir: Path) -> None:
        """Multiple errors from different fields are all reported."""
        cfg = SeedConfig(
            goal="Test",
            context_files=("missing.md",),
            agent_catalog="/nonexistent",
        )
        result = validate_config_paths(cfg, workdir)
        assert not result.ok
        assert len(result.errors) == 2
        fields = {e.field for e in result.errors}
        assert fields == {"context_files", "agent_catalog"}

    def test_all_valid(self, workdir: Path) -> None:
        """All paths valid returns ok."""
        (workdir / "docs").mkdir()
        (workdir / "docs" / "spec.md").write_text("spec")
        (workdir / "catalog").mkdir()
        cfg = SeedConfig(
            goal="Test",
            context_files=("docs/spec.md",),
            agent_catalog="catalog",
        )
        result = validate_config_paths(cfg, workdir)
        assert result.ok


# ---------------------------------------------------------------------------
# check_config_paths tests
# ---------------------------------------------------------------------------


class TestCheckConfigPaths:
    """Tests for check_config_paths entry point."""

    def test_exits_on_missing_path(self, workdir: Path) -> None:
        """SystemExit raised with CONFIG exit code when validation fails."""
        cfg = SeedConfig(goal="Test", context_files=("missing.md",))
        error_mock = MagicMock()
        error_mock.exit_code = 3  # ExitCode.CONFIG

        with (
            patch("bernstein.cli.errors.BernsteinError", return_value=error_mock),
            pytest.raises(SystemExit) as exc_info,
        ):
            check_config_paths(cfg, workdir)

        assert exc_info.value.code == 3  # ExitCode.CONFIG
        error_mock.print.assert_called_once()

    def test_passes_when_all_valid(self, workdir: Path) -> None:
        """No exit when all paths are valid."""
        (workdir / "readme.md").write_text("hello")
        cfg = SeedConfig(goal="Test", context_files=("readme.md",))

        # Should not raise
        check_config_paths(cfg, workdir)

    def test_error_message_contains_path(self, workdir: Path) -> None:
        """Error message includes the missing path."""
        cfg = SeedConfig(goal="Test", context_files=("docs/DESIGN.md",))

        captured_kwargs: dict[str, str] = {}

        def capture_error(**kwargs: str) -> MagicMock:
            captured_kwargs.update(kwargs)
            return MagicMock()

        with (
            patch("bernstein.cli.errors.BernsteinError", side_effect=capture_error),
            pytest.raises(SystemExit),
        ):
            check_config_paths(cfg, workdir)

        assert "docs/DESIGN.md" in captured_kwargs.get("why", "")
