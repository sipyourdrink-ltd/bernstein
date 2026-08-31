"""Tests for ``bernstein trace export`` command functionality.

Covers the ``trace export`` and ``trace verify-projection`` commands and their options.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd


# Helper function to create mock journal files
def create_mock_run_journal(base_path: Path, run_id: str = "test-run-123", *, with_projection: bool = False):
    """Create a mock run journal.jsonl file.

    ``verify-projection`` reads ``projection.otel.json`` off disk and exits 1
    before it reaches anything a test can patch, so a test that wants the
    verification path has to ask for the projection as well as the journal.
    """
    runs_dir = base_path / ".sdd" / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    journal_path = run_dir / "journal.jsonl"
    journal_path.write_text('{"type": "session_start", "ts": 1234567890.0}\n')
    if with_projection:
        # Contents are irrelevant: projection_from_dict is patched in the tests
        # that use this. It only has to exist and parse as JSON.
        (run_dir / "projection.otel.json").write_text("{}")
    return run_dir


def test_trace_export_needs_trace_extra() -> None:
    """``bernstein trace export`` should require the trace extra."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(trace_cmd, ["export", "test-run-123"])
    # The trace extra gate always runs first
    assert result.exit_code == 1, result.output
    assert "The trace extra is required" in result.output


def test_trace_export_projection_no_journal_events() -> None:
    """``bernstein trace verify-projection`` with no events fails gracefully."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create empty run directory (no journal.jsonl)
            runs_dir = Path(".sdd/runs/test-run-123")
            runs_dir.mkdir(parents=True)
            result = runner.invoke(trace_cmd, ["verify-projection", "test-run-123"])
        assert result.exit_code == 1, result.output
        assert "No event journal" in result.output


def test_trace_export_missing_run_id_with_trace_extra_mocked() -> None:
    """``bernstein trace export <RUN_ID>`` with trace extra mocked should show usage."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable so gate passes, then test usage
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            result = runner.invoke(trace_cmd, ["export"])  # no run_id
    # Now should fail with usage error
    assert result.exit_code == 2, result.output
    assert "Usage:" in result.output
    assert "<RUN_ID>" in result.output


def test_trace_export_latest_no_runs_dir() -> None:
    """``bernstein trace export --last`` with no runs dir should fail."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable so gate passes
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            result = runner.invoke(trace_cmd, ["export", "--last"])
    assert result.exit_code == 1, result.output
    assert "No runs directory:" in result.output


def test_trace_export_latest_empty_runs_dir() -> None:
    """``bernstein trace export --last`` with empty runs dir should fail."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable so gate passes
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create empty runs directory
            runs_dir = Path(".sdd") / "runs"
            runs_dir.mkdir(parents=True)
            result = runner.invoke(trace_cmd, ["export", "--last"])
    assert result.exit_code == 1, result.output
    assert "No finished runs found" in result.output


def test_trace_export_create_run_structure_and_succeed() -> None:
    """Create a full run structure and test export success path."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # First make agentrust_trace importable
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create mock run structure with journal
            create_mock_run_journal(Path.cwd(), "test-run-123", with_projection=True)

            # Now mock all downstream dependencies
            with patch("bernstein.core.replay.journal.run_journal_path") as mock_journal_path:
                with patch("bernstein.core.replay.journal.verify_journal") as mock_verify:
                    with patch("bernstein.core.observability.trust_record.TrustRecordEmitter") as mock_emitter:
                        # Configure mocks
                        mock_journal_path.return_value = Path(".sdd/runs/test-run-123/journal.jsonl")
                        mock_verification = MagicMock()
                        mock_verification.chain_consistent = True
                        mock_verify.return_value = mock_verification
                        mock_emitter_instance = MagicMock()
                        mock_emitter_instance.emit_trust_record.return_value = '{"trust": "record"}'
                        mock_emitter.return_value = mock_emitter_instance

                        result = runner.invoke(trace_cmd, ["export", "test-run-123"])

        assert result.exit_code == 0, result.output
        assert '{"trust": "record"}' in result.output


def test_trace_export_create_run_structure_and_output_file() -> None:
    """Test export with --out flag writes to specified file."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create mock run structure with journal
            create_mock_run_journal(Path.cwd(), "test-run-123", with_projection=True)

            # Mock downstream dependencies
            with patch("bernstein.core.replay.journal.run_journal_path") as mock_journal_path:
                with patch("bernstein.core.replay.journal.verify_journal") as mock_verify:
                    with patch("bernstein.core.observability.trust_record.TrustRecordEmitter") as mock_emitter:
                        mock_journal_path.return_value = Path(".sdd/runs/test-run-123/journal.jsonl")
                        mock_verification = MagicMock()
                        mock_verification.chain_consistent = True
                        mock_verify.return_value = mock_verification
                        mock_emitter_instance = MagicMock()
                        mock_emitter_instance.emit_trust_record.return_value = '{"to": "file"}'
                        mock_emitter.return_value = mock_emitter_instance

                        result = runner.invoke(trace_cmd, ["export", "test-run-123", "--out", "output.json"])

        assert result.exit_code == 0, result.output
        assert "Exported trace to:" in result.output
        assert Path("output.json").exists()
        assert Path("output.json").read_text().strip() == '{"to": "file"}'


def test_trace_export_projection_create_run_structure_and_succeed() -> None:
    """Test verify-projection success path."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create mock run structure
            create_mock_run_journal(Path.cwd(), "test-run-123", with_projection=True)

            # Mock OTel projection verification chain
            with patch("bernstein.cli.commands.advanced_cmd._journal_path_for_run") as mock_journal_path:
                with patch("bernstein.core.replay.journal.load_events") as mock_load_events:
                    with patch(
                        "bernstein.core.observability.otel_projection.projection_from_dict"
                    ) as mock_projection_from_dict:
                        with patch(
                            "bernstein.cli.commands.supervisor_cmd._load_or_create_install_key"
                        ) as mock_load_key:
                            with patch(
                                "bernstein.cli.commands._otel_projection_audit.verify_and_render_projection"
                            ) as mock_verify:
                                # Configure all mocks
                                mock_journal_path.return_value = Path(".sdd/runs/test-run-123/journal.jsonl")
                                mock_load_events.return_value.events = [{"test": "event"}]
                                mock_projection = MagicMock()
                                mock_projection_from_dict.return_value = mock_projection
                                mock_load_key.return_value.public_key.return_value = "mock_public_key"
                                mock_verify.return_value = 0  # Success

                                result = runner.invoke(trace_cmd, ["verify-projection", "test-run-123"])

        assert result.exit_code == 0, result.output


def test_trace_export_projection_create_run_structure_and_custom_path() -> None:
    """Test verify-projection with custom projection path."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create mock run structure
            create_mock_run_journal(Path.cwd(), "test-run-123", with_projection=True)

            # Create a custom projection file
            custom_projection = Path("my_projection.otel.json")
            custom_projection.write_text('{"trace_id": "custom", "spans": []}')

            # Mock OTel projection verification chain
            with patch("bernstein.cli.commands.advanced_cmd._journal_path_for_run") as mock_journal_path:
                with patch("bernstein.core.replay.journal.load_events") as mock_load_events:
                    with patch(
                        "bernstein.core.observability.otel_projection.projection_from_dict"
                    ) as mock_projection_from_dict:
                        with patch(
                            "bernstein.cli.commands.supervisor_cmd._load_or_create_install_key"
                        ) as mock_load_key:
                            with patch(
                                "bernstein.cli.commands._otel_projection_audit.verify_and_render_projection"
                            ) as mock_verify:
                                mock_journal_path.return_value = Path(".sdd/runs/test-run-123/journal.jsonl")
                                mock_load_events.return_value.events = [{"test": "event"}]
                                mock_projection = MagicMock()
                                mock_projection_from_dict.return_value = mock_projection
                                mock_load_key.return_value.public_key.return_value = "mock_public_key"
                                mock_verify.return_value = 0  # Success

                                result = runner.invoke(
                                    trace_cmd,
                                    ["verify-projection", "test-run-123", "--projection", "my_projection.otel.json"],
                                )

        assert result.exit_code == 0, result.output


def test_trace_export_projection_verification_fails() -> None:
    """Test verify-projection with verification failure."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Make agentrust_trace importable
        mock_trace_module = MagicMock()
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            # Create mock run structure
            create_mock_run_journal(Path.cwd(), "test-run-123", with_projection=True)

            with patch("bernstein.cli.commands.advanced_cmd._journal_path_for_run") as mock_journal_path:
                with patch("bernstein.core.replay.journal.load_events") as mock_load_events:
                    with patch(
                        "bernstein.core.observability.otel_projection.projection_from_dict"
                    ) as mock_projection_from_dict:
                        with patch(
                            "bernstein.cli.commands.supervisor_cmd._load_or_create_install_key"
                        ) as mock_load_key:
                            with patch(
                                "bernstein.cli.commands._otel_projection_audit.verify_and_render_projection"
                            ) as mock_verify:
                                # Set up to fail verification
                                mock_journal_path.return_value = Path(".sdd/runs/test-run-123/journal.jsonl")
                                mock_load_events.return_value.events = [{"test": "event"}]
                                mock_projection = MagicMock()
                                mock_projection_from_dict.return_value = mock_projection
                                mock_load_key.return_value.public_key.return_value = "mock_public_key"
                                mock_verify.return_value = 2  # Verification failure exit code

                                result = runner.invoke(trace_cmd, ["verify-projection", "test-run-123"])

        assert result.exit_code == 2, result.output
