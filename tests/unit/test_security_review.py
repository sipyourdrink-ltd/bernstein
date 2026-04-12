"""Tests for security review module — pattern scanning on diffs."""

from __future__ import annotations

from bernstein.plugins.security_review import (
    SecurityReviewResult,
    format_security_review,
    run_security_review,
    summarize_security_review,
)

# ---------------------------------------------------------------------------
# run_security_review tests
# ---------------------------------------------------------------------------


class TestRunSecurityReview:
    def test_empty_diff(self) -> None:
        """Empty diff produces zero findings."""
        results = run_security_review("")
        assert results == []

    def test_clean_diff(self) -> None:
        """A normal, clean diff produces zero findings."""
        diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,3 +1,5 @@\n"
            "+def greet(name: str) -> str:\n"
            "+    return f'Hello, {name}!'\n"
            " \n"
            " def main():\n"
            "     print('done')\n"
        )
        results = run_security_review(diff)
        assert results == []

    def test_detects_aws_access_key(self) -> None:
        """Hardcoded AWS access key is detected as critical."""
        diff = "+++ b/src/config.py\n@@ -0,0 +1 @@\n+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE1'\n"
        results = run_security_review(diff)
        assert any(r.severity == "critical" and r.pattern_name == "aws_access_key" for r in results)
        assert any(r.severity == "critical" for r in results)

    def test_detects_private_key(self) -> None:
        """Private key block is detected as critical."""
        diff = "+++ b/src/certs/key.pem\n@@ -0,0 +1 @@\n+-----BEGIN RSA PRIVATE KEY-----\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "private_key_block" and r.severity == "critical" for r in results)

    def test_detects_generic_secret(self) -> None:
        """Hardcoded password/secret assignment is detected."""
        diff = "+++ b/src/app.py\n@@ -0,0 +1 @@\n+password = 'super_secret_value_123!'\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "generic_secret_assignment" and r.severity == "high" for r in results)

    def test_detects_python_eval(self) -> None:
        """eval() usage is detected as high severity."""
        diff = "+++ b/src/parser.py\n@@ -0,0 +1 @@\n+x = eval(user_input)\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "python_eval" for r in results)

    def test_detects_python_exec(self) -> None:
        """exec() usage is detected."""
        diff = "+++ b/src/runner.py\n@@ -0,0 +1 @@\n+exec(compiled_code)\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "python_exec" for r in results)

    def test_detects_shell_injection(self) -> None:
        """subprocess with shell=True is detected."""
        diff = "+++ b/src/shell.py\n@@ -0,0 +1 @@\n+import subprocess\n+subprocess.run(cmd, shell=True)\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "shell_injection_subprocess" for r in results)
        assert any(r.severity == "high" for r in results)

    def test_detects_os_system(self) -> None:
        """os.system() usage is detected."""
        diff = "+++ b/src/run.py\n@@ -0,0 +1 @@\n+import os\n+os.system('rm -rf /tmp')\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "shell_injection_os_system" for r in results)

    def test_detects_weak_crypto_md5(self) -> None:
        """MD5 usage is detected as medium severity."""
        diff = "+++ b/src/hash.py\n@@ -0,0 +1 @@\n+import hashlib\n+h = hashlib.md5(b'test')\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "weak_crypto_md5" and r.severity == "medium" for r in results)

    def test_detects_weak_crypto_sha1(self) -> None:
        """SHA1 usage is detected."""
        diff = "+++ b/src/hash.py\n@@ -0,0 +1 @@\n+h = hashlib.sha1(b'test')\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "weak_crypto_sha1" for r in results)

    def test_detects_sql_string_concat(self) -> None:
        """SQL via string concatenation is detected."""
        diff = "+++ b/src/db.py\n@@ -0,0 +1 @@\n+query = 'SELECT * FROM users WHERE id=' + user_id\n"
        results = run_security_review(diff)
        assert any(
            r.severity == "critical" and ("sql_string_concat" in r.pattern_name or "sql" in r.pattern_name)
            for r in results
        )

    def test_detects_sql_fstring(self) -> None:
        """SQL via f-string is detected."""
        diff = '+++ b/src/db.py\n@@ -0,0 +1 @@\n+query = f"SELECT * FROM users WHERE id={user_id}"\n'
        results = run_security_review(diff)
        assert any("sql_fstring" in r.pattern_name for r in results)

    def test_detects_unsafe_pickle(self) -> None:
        """pickle.load is detected."""
        diff = "+++ b/src/load.py\n@@ -0,0 +1 @@\n+import pickle\n+data = pickle.loads(raw_bytes)\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "unsafe_pickle" for r in results)

    def test_detects_unsafe_yaml_load(self) -> None:
        """yaml.load without Loader is detected."""
        diff = "+++ b/src/config.py\n@@ -0,0 +1 @@\n+import yaml\n+data = yaml.load(f)\n"
        results = run_security_review(diff)
        assert any(r.pattern_name == "unsafe_yaml_load" for r in results)

    def test_detects_path_traversal(self) -> None:
        """Path traversal with '..' is detected."""
        diff = "+++ b/src/file.py\n@@ -0,0 +1 @@\n+os.path.join(base, '../etc/passwd')\n"
        results = run_security_review(diff)
        assert any("path_traversal" in r.pattern_name for r in results)

    def test_result_sorting_by_severity(self) -> None:
        """Results are sorted with critical first."""
        diff = (
            "+++ b/src/app.py\n"
            "@@ -0,0 +1 @@\n"
            "+import hashlib\n"
            "+h = hashlib.md5(b'test')\n"
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            "+password = 'super_secret_value_123!'\n"
        )
        results = run_security_review(diff)
        severities = [r.severity for r in results]
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_severities = sorted(severities, key=lambda s: severity_order[s])
        assert severities == sorted_severities

    def test_file_attribution(self) -> None:
        """Findings attribute to correct file from diff headers."""
        diff = (
            "+++ b/src/a.py\n"
            "@@ -0,0 +1 @@\n"
            "+import os\n"
            "+os.system('echo hi')\n"
            "+++ b/src/b.py\n"
            "@@ -0,0 +1 @@\n"
            "+import hashlib\n"
            "+h = hashlib.sha1(b'x')\n"
        )
        results = run_security_review(diff)
        shell_results = [r for r in results if r.pattern_name == "shell_injection_os_system"]
        sha1_results = [r for r in results if r.pattern_name == "weak_crypto_sha1"]
        if shell_results:
            assert "a.py" in shell_results[0].file
        if sha1_results:
            assert "b.py" in sha1_results[0].file

    def test_result_has_suggestion_and_description(self) -> None:
        """Each result has non-empty description and suggestion."""
        diff = "+++ b/test.py\n@@ -0,0 +1 @@\n+x = eval('1+1')\n"
        results = run_security_review(diff)
        assert len(results) >= 1
        r = next(r for r in results if "eval" in r.pattern_name)
        assert r.description
        assert r.suggestion


# ---------------------------------------------------------------------------
# format_security_review tests
# ---------------------------------------------------------------------------


class TestFormatSecurityReview:
    def test_empty_results_format(self) -> None:
        """Empty results produce a green pass message."""
        output = format_security_review([])
        assert "Security review passed" in output
        assert "green" in output

    def test_non_empty_results_format(self) -> None:
        """Non-empty results produce a formatted report."""
        results = [
            SecurityReviewResult(
                file="src/app.py",
                severity="critical",
                description="AWS key detected",
                suggestion="Use IAM roles",
                pattern_name="aws_access_key",
            )
        ]
        output = format_security_review(results)
        assert "Security review" in output
        assert "1 issue" in output
        assert "src/app.py" in output
        assert "CRITICAL" in output
        assert "Use IAM roles" in output

    def test_summary_section_format(self) -> None:
        """Output includes a Summary section with per-severity counts."""
        results = [
            SecurityReviewResult(
                file="a.py",
                severity="critical",
                description="Key",
                suggestion="Fix",
            ),
            SecurityReviewResult(
                file="a.py",
                severity="high",
                description="Eval",
                suggestion="Fix",
            ),
        ]
        output = format_security_review(results)
        assert "Summary:" in output
        assert "CRITICAL: 1" in output
        assert "HIGH: 1" in output

    def test_line_range_display(self) -> None:
        """Line range is displayed when available."""
        results = [
            SecurityReviewResult(
                file="x.py",
                severity="medium",
                description="MD5 used",
                line_range=(10, 10),
            )
        ]
        output = format_security_review(results)
        assert "line 10" in output


# ---------------------------------------------------------------------------
# summarize_security_review tests
# ---------------------------------------------------------------------------


class TestSummarizeSecurityReview:
    def test_empty_summary(self) -> None:
        """Empty results produce not-blocked summary."""
        summary = summarize_security_review([])
        assert summary.total_findings == 0
        assert not summary.blocked
        assert summary.by_severity == {}

    def test_blocked_summary(self) -> None:
        """Critical or high findings produce blocked=True."""
        results = [
            SecurityReviewResult(file="a.py", severity="critical", description="x"),
        ]
        summary = summarize_security_review(results)
        assert summary.total_findings == 1
        assert summary.blocked
        assert summary.by_severity.get("critical") == 1

    def test_not_blocked_medium_only(self) -> None:
        """Only medium findings do not block."""
        results = [
            SecurityReviewResult(file="a.py", severity="medium", description="x"),
        ]
        summary = summarize_security_review(results)
        assert summary.total_findings == 1
        assert not summary.blocked

    def test_high_finding_blocks(self) -> None:
        """Critical or high both cause blocked=True."""
        results = [
            SecurityReviewResult(file="a.py", severity="high", description="x"),
        ]
        summary = summarize_security_review(results)
        assert summary.blocked

    def test_multi_severity_counts(self) -> None:
        """Multiple severities are correctly counted."""
        results = [
            SecurityReviewResult(file="a.py", severity="critical", description="x"),
            SecurityReviewResult(file="b.py", severity="high", description="x"),
            SecurityReviewResult(file="c.py", severity="medium", description="x"),
            SecurityReviewResult(file="d.py", severity="low", description="x"),
        ]
        summary = summarize_security_review(results)
        assert summary.total_findings == 4
        assert summary.by_severity["critical"] == 1
        assert summary.by_severity["high"] == 1
        assert summary.by_severity["medium"] == 1
        assert summary.by_severity["low"] == 1
