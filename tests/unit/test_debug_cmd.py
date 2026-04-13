"""Tests for the bernstein debug CLI command (#744)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Stub types used when the real observability module is absent.
# ---------------------------------------------------------------------------


@dataclass
class _StubBundleConfig:
    """Minimal stand-in for BundleConfig."""

    workdir: Path
    output_path: Path | None = None
    extended: bool = False


@dataclass
class _StubBundleManifest:
    """Minimal stand-in for BundleManifest."""

    zip_path: Path = Path("bernstein-debug-2026-04-13T12-30-00.zip")
    size_human: str = "42 KB"


# ---------------------------------------------------------------------------
# Helper: build a standalone Click group containing debug_cmd.
# ---------------------------------------------------------------------------

_MODULE = "bernstein.cli.commands.debug_cmd"


def _make_cli() -> click.Group:
    """Return a minimal Click group with `debug_cmd` attached."""
    from bernstein.cli.commands.debug_cmd import debug_cmd

    grp = click.Group("test-cli")
    grp.add_command(debug_cmd, "debug")
    return grp


def _mock_load_available() -> tuple[type[_StubBundleConfig], MagicMock]:
    """Return a (BundleConfig, create_fn) tuple mimicking a successful import."""
    create_fn = MagicMock(return_value=_StubBundleManifest())
    return (_StubBundleConfig, create_fn)


def _mock_load_unavailable() -> tuple[None, None]:
    """Return (None, None) mimicking a missing observability module."""
    return (None, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDebugCmdHelp:
    """--help should render the description without errors."""

    def test_help_shows_description(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--help"])
        assert result.exit_code == 0
        assert "diagnostic bundle" in result.output.lower()

    def test_help_lists_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--help"])
        assert "--yes" in result.output
        assert "--output" in result.output
        assert "--extended" in result.output


class TestDebugCmdConfirmation:
    """Confirmation prompt behaviour."""

    @patch(f"{_MODULE}._load_bundle_module")
    def test_yes_skips_confirmation(self, mock_load: MagicMock) -> None:
        """--yes should bypass the interactive prompt."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code == 0
        assert "Generate debug bundle?" not in result.output
        create_fn.assert_called_once()

    @patch(f"{_MODULE}._load_bundle_module")
    def test_confirm_yes_proceeds(self, mock_load: MagicMock) -> None:
        """Answering 'y' to the prompt should generate the bundle."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug"], input="y\n")
        assert result.exit_code == 0
        create_fn.assert_called_once()

    @patch(f"{_MODULE}._load_bundle_module")
    def test_confirm_no_exits(self, mock_load: MagicMock) -> None:
        """Answering 'n' to the prompt should exit without generating."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug"], input="n\n")
        assert result.exit_code == 0
        assert "Cancelled" in result.output
        create_fn.assert_not_called()

    @patch(f"{_MODULE}._load_bundle_module")
    def test_confirm_empty_defaults_no(self, mock_load: MagicMock) -> None:
        """Empty input (Enter) should default to 'no' and exit."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug"], input="\n")
        assert result.exit_code == 0
        create_fn.assert_not_called()


class TestDebugCmdOutput:
    """--output and default output behaviour."""

    @patch(f"{_MODULE}._load_bundle_module")
    def test_output_custom_path(self, mock_load: MagicMock) -> None:
        """--output should pass the custom path through to BundleConfig."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(
            _make_cli(),
            ["debug", "--yes", "--output", "/tmp/my-debug.zip"],
        )
        assert result.exit_code == 0
        config = create_fn.call_args[0][0]
        assert config.output_path == Path("/tmp/my-debug.zip")

    @patch(f"{_MODULE}._load_bundle_module")
    def test_output_default_none(self, mock_load: MagicMock) -> None:
        """Without --output the config should have output_path=None."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code == 0
        config = create_fn.call_args[0][0]
        assert config.output_path is None


class TestDebugCmdExtended:
    """--extended flag behaviour."""

    @patch(f"{_MODULE}._load_bundle_module")
    def test_extended_flag_passed(self, mock_load: MagicMock) -> None:
        """--extended should set extended=True in BundleConfig."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes", "--extended"])
        assert result.exit_code == 0
        config = create_fn.call_args[0][0]
        assert config.extended is True

    @patch(f"{_MODULE}._load_bundle_module")
    def test_extended_flag_default_false(self, mock_load: MagicMock) -> None:
        """Without --extended, extended should be False."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code == 0
        config = create_fn.call_args[0][0]
        assert config.extended is False


class TestDebugCmdBannerAndOutput:
    """Output text validation."""

    @patch(f"{_MODULE}._load_bundle_module")
    def test_collection_summary_shown(self, mock_load: MagicMock) -> None:
        """The collection summary banner should appear before prompting."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code == 0
        assert "Bernstein Debug Bundle Generator" in result.output
        assert "bernstein.yaml" in result.output
        assert "secrets will be REDACTED" in result.output

    @patch(f"{_MODULE}._load_bundle_module")
    def test_next_steps_shown(self, mock_load: MagicMock) -> None:
        """After generation, next steps should be printed."""
        config_cls, create_fn = _mock_load_available()
        mock_load.return_value = (config_cls, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code == 0
        assert "Next steps:" in result.output
        assert "github.com/chernistry/bernstein/issues" in result.output

    @patch(f"{_MODULE}._load_bundle_module")
    def test_bundle_path_shown(self, mock_load: MagicMock) -> None:
        """The saved bundle path should be printed."""
        manifest = _StubBundleManifest(
            zip_path=Path("my-bundle.zip"),
            size_human="15 KB",
        )
        create_fn = MagicMock(return_value=manifest)
        mock_load.return_value = (_StubBundleConfig, create_fn)
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code == 0
        assert "my-bundle.zip" in result.output
        assert "15 KB" in result.output


class TestDebugCmdMissingModule:
    """Behaviour when create_debug_bundle is not available."""

    @patch(f"{_MODULE}._load_bundle_module")
    def test_missing_module_error(self, mock_load: MagicMock) -> None:
        """If the bundle module is absent, the command should exit with an error."""
        mock_load.return_value = _mock_load_unavailable()
        runner = CliRunner()
        result = runner.invoke(_make_cli(), ["debug", "--yes"])
        assert result.exit_code != 0
        assert "not available" in result.output
