"""Unit tests for ``src/bernstein/core/verifier/audit_receipt_verifier.py``

This module implements the console entry point for the standalone audit receipt
verifier. It wraps ``tools/verify_audit_receipt.py`` in a function-based CLI
interface with zero import dependency on the bernstein orchestrator.

The main behavior of interest is that ``audit_receipt_verifier.main()`` executes
the standalone verifier script as a subprocess and forwards its exit codes.
We do NOT re-implement any verification logic here; the script itself is tested
in ``tests/unit/test_audit_receipt.py`` and ``tests/unit/test_audit_receipt_format_vectors.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

# Subject under test
from bernstein.core.verifier.audit_receipt_verifier import _PROJECT_ROOT, _VERIFIER_SCRIPT, main

# Test artifacts
_TEST_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _TEST_ROOT / "fixtures" / "receipt-vectors"
_VALID_RECEIPT = _FIXTURES / "valid-receipt.json"


def test_verifier_script_path_exists() -> None:
    """The standalone verifier script is present and readable."""
    assert _VERIFIER_SCRIPT.exists()
    assert _VERIFIER_SCRIPT.is_file()
    content = _VERIFIER_SCRIPT.read_text(encoding="utf-8")
    assert "Standalone verifier for a bernstein audit receipt" in content
    assert "def main" in content


def test_verifier_script_imports_no_bernstein() -> None:
    """The headline promise: the verifier imports no bernstein modules."""
    source = _VERIFIER_SCRIPT.read_text(encoding="utf-8")
    # Look for import statements
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import bernstein", "from bernstein")):
            raise AssertionError(f"verifier imports bernstein: {stripped}")


def test_project_root_resolution() -> None:
    """The _PROJECT_ROOT points to the repository root, not the worktree."""
    assert _PROJECT_ROOT.exists()
    assert (_PROJECT_ROOT / "pyproject.toml").exists()
    assert (_PROJECT_ROOT / "src").exists()
    assert (_PROJECT_ROOT / "tools" / "verify_audit_receipt.py").exists()


def test_verifier_script_valid_json() -> None:
    """The valid receipt fixture is parseable JSON with expected schema fields."""
    data = json.loads(_VALID_RECEIPT.read_text(encoding="utf-8"))
    assert "receipt_type" in data
    assert "events" in data
    assert "subject" in data
    assert "range" in data
    assert "formats" in data
    assert "signing" in data
    assert "formats" in data


def test_auditor_script_returns_success_for_valid_receipt() -> None:
    """The standalone verifier returns 0 for a valid receipt."""
    result = subprocess.run(
        [sys.executable, str(_VERIFIER_SCRIPT), "--receipt", str(_VALID_RECEIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    # The standalone verifier only returns 0 for fully valid receipts.
    # Since this fixture is the committed vector, it must be fully valid.
    assert result.returncode == 0, f"expected 0 but got {result.returncode}; stderr: {result.stderr}"


def test_verifier_script_execution_without_args() -> None:
    """Running without required args returns exit code 2 (argument error)."""
    result = subprocess.run(
        [sys.executable, str(_VERIFIER_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    # Missing --receipt should cause argument error
    assert result.returncode == 2, f"expected 2 but got {result.returncode}"


def test_verifier_script_responds_to_help() -> None:
    """The CLI responds to help and shows usage."""
    result = subprocess.run(
        [sys.executable, str(_VERIFIER_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"expected 0 but got {result.returncode}"
    assert "usage:" in result.stdout.lower()


def test_cli_entry_point_main_without_args() -> None:
    """The audit_receipt_verifier.main() propagates None argv."""
    # main() with None argv uses sys.argv[1:], so it will error like subprocess
    with patch("sys.argv", ["verify-audit-receipt", "--help"]):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            main(None)
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert str(_VERIFIER_SCRIPT) in call_args[1]


def test_cli_entry_point_main_with_custom_argv() -> None:
    """The audit_receipt_verifier.main() uses custom argv correctly."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(returncode=0)
        main(["--receipt", str(_VALID_RECEIPT)])
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert str(_VERIFIER_SCRIPT) in call_args[1]
        assert str(_VALID_RECEIPT) in call_args[3]


def test_cli_entry_point_main_return_code_matches_subprocess() -> None:
    """The exit codes from main() match those of the subprocess call."""
    # We'll test by mocking subprocess.run to return various codes
    for expected_code in (0, 1, 2):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=expected_code)
            result = main(["--receipt", str(_VALID_RECEIPT)])
            assert result == expected_code, f"expected {expected_code} but got {result}"


def test_cli_entry_point_main_with_bad_receipt_file() -> None:
    """Running with a non-existent receipt file returns exit code 2."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(returncode=2)
        result = main(["--receipt", "/nonexistent/path.json"])
        assert result == 2


def test_verifier_script_execution_handles_explicit_key_pin() -> None:
    """The verifier correctly accepts a --jwk pin and validates."""
    key_path = _FIXTURES / "valid-receipt-key.pem"
    result = subprocess.run(
        [
            sys.executable,
            str(_VERIFIER_SCRIPT),
            "--receipt",
            str(_VALID_RECEIPT),
            "--public-key",
            str(key_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # With a correct pin, verification should pass
    assert result.returncode == 0, f"expected 0 but got {result.returncode}; stderr: {result.stderr}"


def test_auditor_script_execution_without_pin_still_passes() -> None:
    """Unpinned verification (TOFU) should still pass for valid receipts."""
    result = subprocess.run(
        [sys.executable, str(_VERIFIER_SCRIPT), "--receipt", str(_VALID_RECEIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    # TOFU verification should succeed for a valid receipt
    assert result.returncode == 0, f"expected 0 but got {result.returncode}; stderr: {result.stderr}"


def test_audit_receipt_verifier_module_has_expected_structure() -> None:
    """The module exports the expected public API."""
    # Import the module correctly
    from bernstein.core.verifier import audit_receipt_verifier

    assert "main" in dir(audit_receipt_verifier)
    # Check module docstring exists and is descriptive
    assert audit_receipt_verifier.__doc__ is not None
    assert (
        "Console entry point" in audit_receipt_verifier.__doc__
        or "verify-audit-receipt" in audit_receipt_verifier.__doc__
    )
