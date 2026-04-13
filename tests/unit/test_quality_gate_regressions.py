"""TEST-007: Quality gate regression tests — gates that should fail DO fail.

Negative test cases verifying that deliberately bad code (syntax errors,
type violations, failing tests, leaked secrets) correctly triggers gate
failures.  These complement the passing-case tests in test_quality_gates.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.pii_output_gate import scan_text
from bernstein.core.quality_gates import (
    QualityGatesConfig,
    _parse_mutation_score,
    _run_command,
    _run_pii_gate,
    run_quality_gates,
)


def _make_task(*, id: str = "T-reg-001", role: str = "backend") -> Task:
    return Task(
        id=id,
        title="Regression task",
        description="Test gate regressions.",
        role=role,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
    )


# ---------------------------------------------------------------------------
# TEST-007a: Lint gate — real ruff violations trigger failure
# ---------------------------------------------------------------------------


class TestLintGateRegressions:
    """Lint gate catches real code violations, not just exit-code stubs."""

    def test_real_ruff_violation_blocks_lint_gate(self, tmp_path: Path) -> None:
        """A Python file with an F821 undefined-name error causes ruff to exit non-zero."""
        bad_py = tmp_path / "bad_code.py"
        bad_py.write_text(
            textwrap.dedent("""\
                # This file has a clear ruff/pyflakes violation: undefined name
                x = undefined_variable_that_does_not_exist
            """),
            encoding="utf-8",
        )

        ok, output = _run_command(f"ruff check {bad_py}", tmp_path, timeout_s=30)
        assert not ok, f"Expected ruff to fail on undefined name, but it passed. Output: {output}"

    def test_unused_import_triggers_ruff(self, tmp_path: Path) -> None:
        """An unused import (F401) causes ruff to exit non-zero."""
        bad_py = tmp_path / "unused_import.py"
        bad_py.write_text("import os\nimport sys\n\nx = 1\n", encoding="utf-8")

        ok, output = _run_command(f"ruff check {bad_py}", tmp_path, timeout_s=30)
        assert not ok, f"Expected ruff to fail on unused imports, but it passed. Output: {output}"

    def test_clean_python_passes_lint(self, tmp_path: Path) -> None:
        """A clean Python file passes the lint gate — baseline control test."""
        clean_py = tmp_path / "clean_code.py"
        clean_py.write_text(
            '"""Clean module."""\n\n\ndef add(a: int, b: int) -> int:\n    """Add two numbers."""\n    return a + b\n',
            encoding="utf-8",
        )

        ok, _output = _run_command(f"ruff check {clean_py}", tmp_path, timeout_s=30)
        assert ok, "Expected clean Python to pass ruff check"

    def test_syntax_error_in_python_blocks_lint_gate(self, tmp_path: Path) -> None:
        """A Python file with a syntax error causes ruff to exit non-zero."""
        bad_py = tmp_path / "syntax_error.py"
        bad_py.write_text("def broken(\n    # missing closing paren\nx = 1\n", encoding="utf-8")

        ok, output = _run_command(f"ruff check {bad_py}", tmp_path, timeout_s=30)
        assert not ok, f"Expected ruff to fail on syntax error. Output: {output}"

    def test_lint_gate_failure_propagates_through_run_quality_gates(self, tmp_path: Path) -> None:
        """End-to-end: a real ruff violation blocks the gate pipeline."""
        bad_py = tmp_path / "src" / "bad.py"
        bad_py.parent.mkdir(parents=True, exist_ok=True)
        bad_py.write_text("import os\n\nx = 1\n", encoding="utf-8")

        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command=f"ruff check {bad_py}",
            type_check=False,
            tests=False,
            pii_scan=False,
        )
        task = _make_task(id="T-lint-regression")
        result = run_quality_gates(task, tmp_path, tmp_path, config)

        assert not result.passed
        lint_result = next(r for r in result.gate_results if r.gate == "lint")
        assert not lint_result.passed
        assert lint_result.blocked


# ---------------------------------------------------------------------------
# TEST-007b: PII / secret scan gate — leaked secrets trigger failure
# ---------------------------------------------------------------------------


class TestPiiGateRegressions:
    """PII gate correctly identifies and blocks real secrets."""

    def test_aws_access_key_blocks_pii_gate(self, tmp_path: Path) -> None:
        """A file containing an AWS access key ID causes the PII gate to block."""
        secret_py = tmp_path / "src" / "config.py"
        secret_py.parent.mkdir(parents=True, exist_ok=True)
        secret_py.write_text(
            textwrap.dedent("""\
                # Production config — DO NOT COMMIT
                AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
                AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            """),
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=[],  # No allowlist — must catch real keys
            lint=False,
            type_check=False,
            tests=False,
        )
        result = _run_pii_gate(config, tmp_path)

        assert result.blocked, f"PII gate should block AWS key. Detail: {result.detail}"
        assert not result.passed

    def test_github_token_blocks_pii_gate(self, tmp_path: Path) -> None:
        """A file containing a GitHub PAT causes the PII gate to block."""
        secret_py = tmp_path / "src" / "github_client.py"
        secret_py.parent.mkdir(parents=True, exist_ok=True)
        secret_py.write_text(
            'GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"\n',
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=[],
            lint=False,
            type_check=False,
            tests=False,
        )
        result = _run_pii_gate(config, tmp_path)

        assert result.blocked, f"PII gate should block GitHub token. Detail: {result.detail}"

    def test_hardcoded_password_blocks_pii_gate(self, tmp_path: Path) -> None:
        """A hardcoded password assignment causes the PII gate to block."""
        secret_py = tmp_path / "src" / "db.py"
        secret_py.parent.mkdir(parents=True, exist_ok=True)
        secret_py.write_text(
            'DB_PASSWORD = "s3cr3tP@ssw0rd!"\n',
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=[],
            lint=False,
            type_check=False,
            tests=False,
        )
        result = _run_pii_gate(config, tmp_path)

        assert result.blocked, f"PII gate should block hardcoded password. Detail: {result.detail}"

    def test_pii_gate_passes_clean_code(self, tmp_path: Path) -> None:
        """A file with no secrets passes the PII gate — baseline control test."""
        clean_py = tmp_path / "src" / "utils.py"
        clean_py.parent.mkdir(parents=True, exist_ok=True)
        clean_py.write_text(
            textwrap.dedent("""\
                \"\"\"Utility functions.\"\"\"


                def greet(name: str) -> str:
                    \"\"\"Return a greeting.\"\"\"
                    return f"Hello, {name}!"
            """),
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=[],
            lint=False,
            type_check=False,
            tests=False,
        )
        result = _run_pii_gate(config, tmp_path)

        assert result.passed
        assert not result.blocked

    def test_connection_string_with_credentials_blocks(self, tmp_path: Path) -> None:
        """A database connection string with embedded credentials blocks the gate."""
        secret_py = tmp_path / "src" / "database.py"
        secret_py.parent.mkdir(parents=True, exist_ok=True)
        # Use postgres:// (not postgresql://) to match the detector pattern
        secret_py.write_text(
            'DATABASE_URL = "postgres://admin:s3cr3tpassword@prod-db.internal:5432/mydb"\n',
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=[],
            lint=False,
            type_check=False,
            tests=False,
        )
        result = _run_pii_gate(config, tmp_path)

        assert result.blocked, f"PII gate should block connection string with credentials. Detail: {result.detail}"

    def test_pii_allowlist_exempts_test_values(self, tmp_path: Path) -> None:
        """Values in the allowlist (FAKE, TEST) are not blocked even if they look like secrets."""
        fake_py = tmp_path / "src" / "test_fixtures.py"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text(
            'GITHUB_TOKEN = "FAKE_ghp_1234567890abcdefghijklmnopqrstuvwxyz"\n',
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=["FAKE", "TEST"],
            lint=False,
            type_check=False,
            tests=False,
        )
        result = _run_pii_gate(config, tmp_path)

        # Allowlisted prefixes should prevent blocking
        assert not result.blocked


# ---------------------------------------------------------------------------
# TEST-007c: PII scan_text unit — raw detection coverage
# ---------------------------------------------------------------------------


class TestScanTextRegressions:
    """scan_text() catches each secret category and returns high-severity findings."""

    def test_detects_aws_access_key(self) -> None:
        findings = scan_text('key = "AKIAIOSFODNN7EXAMPLE"', path="config.py")
        high = [f for f in findings if f.severity == "high"]
        assert high, "Expected AWS access key to be detected"
        assert any(f.rule == "aws_access_key" for f in high)

    def test_detects_github_token(self) -> None:
        findings = scan_text('TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"', path="auth.py")
        high = [f for f in findings if f.severity == "high"]
        assert high, "Expected GitHub token to be detected"

    def test_detects_private_key_pem(self) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n"
        findings = scan_text(content, path="key.pem")
        high = [f for f in findings if f.severity == "high"]
        assert high, "Expected PEM private key to be detected"

    def test_detects_stripe_live_key(self) -> None:
        findings = scan_text('STRIPE_KEY = "sk_live_abcdefghijklmnopqrstu"', path="billing.py")
        high = [f for f in findings if f.severity == "high"]
        assert high, "Expected Stripe live key to be detected"

    def test_clean_code_has_no_high_severity_findings(self) -> None:
        clean = textwrap.dedent("""\
            \"\"\"Utility module.\"\"\"


            def compute(x: int) -> int:
                \"\"\"Double the input.\"\"\"
                return x * 2
        """)
        findings = scan_text(clean, path="utils.py")
        high = [f for f in findings if f.severity == "high"]
        assert not high, f"Expected no high-severity findings in clean code, got: {high}"

    def test_example_com_email_not_flagged_high(self) -> None:
        """Emails at allowlisted domains should not be high severity."""
        findings = scan_text('contact = "user@example.com"', path="readme.py")
        high = [f for f in findings if f.severity == "high"]
        assert not high, "example.com email should not produce high-severity findings"


# ---------------------------------------------------------------------------
# TEST-007d: Mutation score parsing — below-threshold triggers failure
# ---------------------------------------------------------------------------


class TestMutationScoreRegressions:
    """Quality gate correctly fails when mutation score is below threshold."""

    @pytest.mark.parametrize(
        "output,threshold,should_pass",
        [
            # mutmut format: killed/total
            ("🎉 10/100  🤔 0  🙁 90  🔇 0", 0.50, False),  # 10% < 50% threshold → fail
            ("🎉 90/100  🤔 0  🙁 10  🔇 0", 0.50, True),  # 90% >= 50% threshold → pass
            ("🎉 50/100  🤔 0  🙁 50  🔇 0", 0.50, True),  # 50% == 50% threshold → pass (>=)
            ("🎉 49/100  🤔 0  🙁 51  🔇 0", 0.50, False),  # 49% < 50% threshold → fail
            # mutatest format: keyword based
            ("Killed: 30\nSurvived: 70", 0.50, False),  # 30% < 50% → fail
            ("Killed: 80\nSurvived: 20", 0.75, True),  # 80% >= 75% → pass
            ("Killed: 74\nSurvived: 26", 0.75, False),  # 74% < 75% → fail
        ],
        ids=[
            "mutmut_10pct_below_50_fails",
            "mutmut_90pct_above_50_passes",
            "mutmut_50pct_equal_threshold_passes",
            "mutmut_49pct_below_50_fails",
            "mutatest_30pct_below_50_fails",
            "mutatest_80pct_above_75_passes",
            "mutatest_74pct_below_75_fails",
        ],
    )
    def test_mutation_score_threshold(self, output: str, threshold: float, should_pass: bool) -> None:
        score = _parse_mutation_score(output)
        assert score is not None, f"Expected parseable mutation score, got None for: {output!r}"
        passed = score >= threshold
        assert passed == should_pass, (
            f"score={score:.2%} threshold={threshold:.2%}: "
            f"expected {'pass' if should_pass else 'fail'}, got {'pass' if passed else 'fail'}"
        )

    def test_unparseable_output_returns_none(self) -> None:
        """Output that contains no mutation score format returns None."""
        score = _parse_mutation_score("All done! No mutations found.")
        assert score is None, f"Expected None for unparseable output, got {score}"

    def test_zero_mutants_returns_none(self) -> None:
        """Zero total mutants is undefined — returns None (not a score)."""
        score = _parse_mutation_score("🎉 0/0  🤔 0  🙁 0  🔇 0")
        assert score is None, f"Expected None for zero-mutant run, got {score}"


# ---------------------------------------------------------------------------
# TEST-007e: Test gate — failing pytest triggers gate failure
# ---------------------------------------------------------------------------


class TestTestGateRegressions:
    """Test gate correctly fails when the test suite has failures."""

    def test_failing_pytest_blocks_test_gate(self, tmp_path: Path) -> None:
        """A test file with a deliberate assertion failure causes the gate to fail."""
        conftest = tmp_path / "conftest.py"
        conftest.write_text("", encoding="utf-8")

        test_file = tmp_path / "test_deliberately_failing.py"
        test_file.write_text(
            textwrap.dedent("""\
                def test_this_must_fail() -> None:
                    \"\"\"Deliberately failing test for regression gate testing.\"\"\"
                    assert False, "This test is supposed to fail"
            """),
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            tests=True,
            test_command=f"python -m pytest {test_file} -q --no-header",
            lint=False,
            type_check=False,
            pii_scan=False,
        )
        task = _make_task(id="T-test-regression")
        result = run_quality_gates(task, tmp_path, tmp_path, config)

        assert not result.passed
        test_result = next(r for r in result.gate_results if r.gate == "tests")
        assert not test_result.passed
        assert test_result.blocked

    def test_passing_pytest_allows_gate(self, tmp_path: Path) -> None:
        """A test file with all passing tests allows the gate through — control test."""
        test_file = tmp_path / "test_passing.py"
        test_file.write_text(
            textwrap.dedent("""\
                def test_always_passes() -> None:
                    \"\"\"Deliberately passing test.\"\"\"
                    assert 1 + 1 == 2
            """),
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            tests=True,
            test_command=f"python -m pytest {test_file} -q --no-header",
            lint=False,
            type_check=False,
            pii_scan=False,
        )
        task = _make_task(id="T-test-passing")
        result = run_quality_gates(task, tmp_path, tmp_path, config)

        assert result.passed

    def test_syntax_error_in_test_file_blocks_gate(self, tmp_path: Path) -> None:
        """A test file with a syntax error causes pytest collection error, blocking the gate."""
        test_file = tmp_path / "test_syntax_error.py"
        test_file.write_text(
            "def test_broken(\n    # missing close paren — syntax error\nx = 1\n",
            encoding="utf-8",
        )

        config = QualityGatesConfig(
            enabled=True,
            tests=True,
            test_command=f"python -m pytest {test_file} -q --no-header",
            lint=False,
            type_check=False,
            pii_scan=False,
        )
        task = _make_task(id="T-test-syntax-error")
        result = run_quality_gates(task, tmp_path, tmp_path, config)

        assert not result.passed
        test_result = next(r for r in result.gate_results if r.gate == "tests")
        assert test_result.blocked


# ---------------------------------------------------------------------------
# TEST-007f: Multiple failing gates — all gates run even when first fails
# ---------------------------------------------------------------------------


class TestMultipleGateRegressions:
    """When multiple gates are configured and one fails, all gates still run."""

    def test_pii_failure_does_not_skip_lint_gate(self, tmp_path: Path) -> None:
        """Even when PII scan blocks, the lint gate still runs and is checked."""
        # Create a src/ with a secret — PII gate will block
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "secret.py").write_text(
            'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n',
            encoding="utf-8",
        )
        # Also create a file with lint issues
        bad_py = tmp_path / "bad_lint.py"
        bad_py.write_text("import os\n\nx = 1\n", encoding="utf-8")

        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command=f"ruff check {bad_py}",
            type_check=False,
            tests=False,
            pii_scan=True,
            pii_scan_paths=["src/"],
            pii_allowlist_prefixes=[],
        )
        task = _make_task(id="T-multi-fail")
        result = run_quality_gates(task, tmp_path, tmp_path, config)

        assert not result.passed
        gate_names = [r.gate for r in result.gate_results]
        # Both gates must have run
        assert "lint" in gate_names, f"Lint gate not found in results: {gate_names}"
        assert "pii_scan" in gate_names, f"PII gate not found in results: {gate_names}"
