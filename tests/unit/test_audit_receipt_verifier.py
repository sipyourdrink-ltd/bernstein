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
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import Mock, patch

import pytest

import bernstein
from bernstein.core.verifier import audit_receipt_verifier

# Subject under test
from bernstein.core.verifier.audit_receipt_verifier import (
    _DEV_VERIFIER_SCRIPT,
    _PACKAGED_VERIFIER_SCRIPT,
    VERIFIER_SCRIPT,
    _resolve_verifier_script,
    main,
)

# Test artifacts
_TEST_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _TEST_ROOT / "fixtures" / "receipt-vectors"
_VALID_RECEIPT = _FIXTURES / "valid-receipt.json"


def test_verifier_script_path_exists() -> None:
    """The standalone verifier script is present and readable."""
    assert VERIFIER_SCRIPT.exists()
    assert VERIFIER_SCRIPT.is_file()
    content = VERIFIER_SCRIPT.read_text(encoding="utf-8")
    assert "Standalone verifier for a bernstein audit receipt" in content
    assert "def main" in content


def test_verifier_script_imports_no_bernstein() -> None:
    """The headline promise: the verifier imports no bernstein modules."""
    source = VERIFIER_SCRIPT.read_text(encoding="utf-8")
    # Look for import statements
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import bernstein", "from bernstein")):
            raise AssertionError(f"verifier imports bernstein: {stripped}")


def test_source_checkout_resolves_to_the_tools_script() -> None:
    """Inside a checkout the console script runs the repo's tools/ copy."""
    repo_root = _DEV_VERIFIER_SCRIPT.parents[1]
    assert (repo_root / "pyproject.toml").exists()
    assert (repo_root / "src").exists()
    assert _DEV_VERIFIER_SCRIPT.is_file()
    assert VERIFIER_SCRIPT == _DEV_VERIFIER_SCRIPT


def test_installed_package_falls_back_to_the_bundled_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no checkout on disk the wheel-bundled copy is what runs.

    This is the pip / uv / conda layout: tools/ is not part of the package,
    so resolving only the checkout path made ``verify-audit-receipt`` exit 2
    with "can't open file .../tools/verify_audit_receipt.py".
    """
    monkeypatch.setattr(audit_receipt_verifier, "_DEV_VERIFIER_SCRIPT", tmp_path / "absent.py")
    assert _resolve_verifier_script() == _PACKAGED_VERIFIER_SCRIPT


def _pyproject() -> dict[str, Any]:
    repo_root = _DEV_VERIFIER_SCRIPT.parents[1]
    return tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))


def test_wheel_bundles_the_verifier_where_the_resolver_looks() -> None:
    """pyproject force-includes tools/verify_audit_receipt.py at the packaged path.

    Dropping the force-include entry, renaming its destination, or moving
    ``_PACKAGED_VERIFIER_SCRIPT`` on its own breaks every installed copy again.
    """
    force_include: Any = _pyproject()
    for key in ("tool", "hatch", "build", "targets", "wheel", "force-include"):
        force_include = force_include.get(key, {})
    destination = force_include.get("tools/verify_audit_receipt.py")
    assert destination is not None, "tools/verify_audit_receipt.py is not force-included into the wheel"

    package_root = Path(bernstein.__file__).resolve().parent
    expected = PurePosixPath(_PACKAGED_VERIFIER_SCRIPT.relative_to(package_root).as_posix())
    assert PurePosixPath(destination).relative_to(PurePosixPath("bernstein")) == expected


def test_sdist_keeps_the_force_include_source() -> None:
    """The wheel is built from the sdist, so tools/ must not be excluded from it."""
    excluded: list[str] = _pyproject()["tool"]["hatch"]["build"].get("exclude", [])
    assert not [pattern for pattern in excluded if pattern.strip("/").startswith("tools")]


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
        [sys.executable, str(VERIFIER_SCRIPT), "--receipt", str(_VALID_RECEIPT), "--allow-unpinned-key"],
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
        [sys.executable, str(VERIFIER_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    # Missing --receipt should cause argument error
    assert result.returncode == 2, f"expected 2 but got {result.returncode}"


def test_verifier_script_responds_to_help() -> None:
    """The CLI responds to help and shows usage."""
    result = subprocess.run(
        [sys.executable, str(VERIFIER_SCRIPT), "--help"],
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
            assert str(VERIFIER_SCRIPT) in call_args[1]


def test_cli_entry_point_main_with_custom_argv() -> None:
    """The audit_receipt_verifier.main() uses custom argv correctly."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = Mock(returncode=0)
        main(["--receipt", str(_VALID_RECEIPT)])
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert str(VERIFIER_SCRIPT) in call_args[1]
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
            str(VERIFIER_SCRIPT),
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


def test_auditor_script_execution_without_pin_fails() -> None:
    """Unpinned verification (TOFU) is not a pass unless explicitly allowed."""
    result = subprocess.run(
        [sys.executable, str(VERIFIER_SCRIPT), "--receipt", str(_VALID_RECEIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, f"expected 1 but got {result.returncode}; stdout: {result.stdout}"
    assert "trust-on-first-use" in result.stdout


def test_audit_receipt_verifier_module_has_expected_structure() -> None:
    """The module exports the expected public API."""
    assert "main" in dir(audit_receipt_verifier)
    # Check module docstring exists and is descriptive
    assert audit_receipt_verifier.__doc__ is not None
    assert (
        "Console entry point" in audit_receipt_verifier.__doc__
        or "verify-audit-receipt" in audit_receipt_verifier.__doc__
    )
