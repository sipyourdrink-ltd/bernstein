"""Security review: regex-based pattern scanning for agent-produced diffs.

Provides lightweight, fast security analysis without LLM calls.  Scans
diff output for common vulnerability patterns including:

- Hardcoded secrets and credentials
- Unsafe eval/exec usage
- Shell injection risks
- Weak cryptographic algorithms
- Path traversal patterns
- SQL injection vectors

Used by quality gates to surface actionable security issues in
agent-produced changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Types and data structures
# ---------------------------------------------------------------------------

Severity = Literal["critical", "high", "medium", "low"]


@dataclass(frozen=True)
class SecurityReviewResult:
    """Single security finding from pattern scanning.

    Attributes:
        file: File path where the issue was found.
        severity: Severity level (critical, high, medium, low).
        description: Human-readable description of the issue.
        line_range: Tuple of (start_line, end_line) or None if unknown.
        suggestion: Remediation advice for the developer.
        pattern_name: Name of the matching pattern.
        matched_text: The actual text that triggered the rule (truncated).
    """

    file: str
    severity: Severity
    description: str
    line_range: tuple[int, int] | None = None
    suggestion: str = ""
    pattern_name: str = ""
    matched_text: str = ""


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# (pattern_name, regex, severity, description_template, suggestion_template)
_SECURITY_PATTERNS: list[tuple[str, re.Pattern[str], Severity, str, str]] = [
    # Hardcoded secrets
    (
        "aws_access_key",
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "critical",
        "Hardcoded AWS access key detected",
        "Use environment variables, AWS IAM roles, or a secrets manager (e.g. AWS Secrets Manager).",
    ),
    (
        "aws_secret_key",
        re.compile(r"(?i)(?:aws_secret_access_key|aws_secret_key)\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
        "critical",
        "Hardcoded AWS secret key detected",
        "Store AWS credentials in a secrets manager or use IAM role-based access.",
    ),
    (
        "generic_secret_assignment",
        re.compile(
            r"(?i)(?:password|passwd|secret|api_key|api_token|access_token|auth_token)\s*[=:]\s*['\"][^\s'\"]{8,}['\"]"
        ),
        "high",
        "Hardcoded secret or credential in source code",
        "Use environment variables or a secrets manager for sensitive values.",
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        "critical",
        "Private key embedded in source code",
        "Remove private keys from code; load from a secure vault or file with restricted permissions.",
    ),
    # Unsafe eval/exec
    (
        "python_eval",
        re.compile(r"\beval\s*\("),
        "high",
        "Use of eval() — arbitrary code execution risk",
        "Replace eval() with ast.literal_eval() for data parsing, or use a safe alternative.",
    ),
    (
        "python_exec",
        re.compile(r"\bexec\s*\("),
        "high",
        "Use of exec() — arbitrary code execution risk",
        "Remove exec(); refactor to use safer constructs like import or function dispatch.",
    ),
    (
        "javascript_eval",
        re.compile(r"(?<!\.)\beval\s*\("),
        "high",
        "JavaScript eval() usage — XSS / code injection risk",
        "Avoid eval() with untrusted input; use JSON.parse() or template engines instead.",
    ),
    # Shell injection
    (
        "shell_injection_subprocess",
        re.compile(r"subprocess\.\w+\s*\([^)]*shell\s*=\s*True"),
        "high",
        "subprocess call with shell=True — shell injection risk",
        "Avoid shell=True; pass arguments as a list: subprocess.run(['cmd', 'arg1'], ...).",
    ),
    (
        "shell_injection_os_system",
        re.compile(r"os\.system\s*\("),
        "high",
        "os.system() call — shell injection risk",
        "Replace os.system() with subprocess.run() using argument lists.",
    ),
    (
        "shell_injection_popen",
        re.compile(r"os\.popen\s*\("),
        "high",
        "os.popen() call — shell injection risk",
        "Replace os.popen() with subprocess.run() using argument lists.",
    ),
    # Weak cryptography
    (
        "weak_crypto_md5",
        re.compile(r"\b(hashlib\.md5|MD5\.new|MD5\.new\(\))\b"),
        "medium",
        "MD5 hash usage — cryptographically broken",
        "Use hashlib.sha256() or hashlib.sha3_256() for security-sensitive hashing.",
    ),
    (
        "weak_crypto_sha1",
        re.compile(r"\b(hashlib\.sha1|SHA1\.new)\b"),
        "medium",
        "SHA1 hash usage — deprecated for security use",
        "Use hashlib.sha256() or hashlib.sha3_256() for security-sensitive hashing.",
    ),
    (
        "weak_crypto_des",
        re.compile(r"\b(DES\.new|DES3\.new|pycryptodome\.des|Crypto\.Cipher\.DES)\b"),
        "high",
        "DES/3DES encryption — weak cipher",
        "Use AES (AES.new()) or ChaCha20 for encryption.",
    ),
    # Path traversal
    (
        "path_traversal_open",
        re.compile(r"open\s*\(\s*(?:f['\"]|.*\+\s*|.*\.format|.*%s)"),
        "medium",
        "File open with dynamic path — potential path traversal",
        "Use os.path.abspath() or pathlib.Path.resolve() to validate paths before opening.",
    ),
    (
        "path_traversal_join",
        re.compile(r"os\.path\.join\s*\([^)]*\.\."),
        "medium",
        "Path join with '..' — potential directory escape",
        "Validate the resolved path stays within the expected base directory.",
    ),
    # SQL injection
    (
        "sql_string_concat",
        re.compile(r"""(?i)(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b.*["'][^"']*["']?\s*(?:\+|%|\.format\()"""),
        "critical",
        "SQL query built via string concatenation/formatting — SQL injection risk",
        "Use parameterized queries: cursor.execute('SELECT ... WHERE id = %s', (user_id,)).",
    ),
    (
        "sql_fstring",
        re.compile(r"""(?i)f['\"](?:SELECT|INSERT|UPDATE|DELETE|DROP)\b"""),
        "critical",
        "SQL query built via f-string — SQL injection risk",
        "Use parameterized queries instead of f-strings for SQL.",
    ),
    # Deserialization risks
    (
        "unsafe_pickle",
        re.compile(r"pickle\.loads?\s*\("),
        "high",
        "pickle.load(s) — arbitrary code execution via crafted pickle",
        "Use json or msgpack for untrusted data; never unpickle untrusted input.",
    ),
    (
        "unsafe_yaml_load",
        re.compile(r"yaml\.load\s*\([^)]*\)(?!.*Loader)"),
        "high",
        "yaml.load() without safe Loader — arbitrary code execution risk",
        "Use yaml.safe_load() or yaml.load(data, Loader=yaml.SafeLoader).",
    ),
]

# Diff context helpers
_DIFF_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_DIFF_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.MULTILINE)
_DIFF_ADD_LINE_PREFIX = re.compile(r"^\+")


def _extract_context(
    diff_text: str, pattern: re.Pattern[str], match: re.Match[str]
) -> tuple[tuple[int, int] | None, str]:
    """Extract line range and matched text from a diff context.

    Args:
        diff_text: The full diff string.
        pattern: The regex pattern that matched.
        match: The regex match object.

    Returns:
        Tuple of (line_range, truncated_matched_text).
        line_range is (start_line, end_line) or None.
    """
    # Find the position of the match within the diff
    pos = match.start()
    before_match = diff_text[:pos]

    # Determine current line number by counting hunk changes
    # This is best-effort: walk forward from the last hunk before the match
    line_num: int | None = None
    hunk_matches = list(_DIFF_HUNK_HEADER.finditer(before_match))
    if hunk_matches:
        last_hunk = hunk_matches[-1]
        start_line = int(last_hunk.group(1))
        # Count added lines from the hunk start to the match position
        segment = before_match[last_hunk.end() : pos]
        added_lines = sum(1 for line in segment.splitlines() if line.startswith("+"))
        line_num = start_line + added_lines

    # Get context around the match
    start = max(0, match.start() - 20)
    end = min(len(diff_text), match.end() + 20)
    matched_snippet = diff_text[start:end]

    line_range = (line_num, line_num) if line_num is not None else None
    return line_range, matched_snippet


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_security_review(diff_text: str) -> list[SecurityReviewResult]:
    """Scan a git diff for known security vulnerability patterns.

    Examines the diff text against a catalogue of regex patterns covering
    hardcoded secrets, unsafe eval/exec, shell injection, weak crypto,
    path traversal, SQL injection, and unsafe deserialization.

    Args:
        diff_text: Git diff output as a string.

    Returns:
        List of SecurityReviewResult, one per matched pattern.
        Results are sorted by severity (critical first).
    """
    results: list[SecurityReviewResult] = []

    # Walk diff to determine file context per-region
    file_regions: list[tuple[str, int, int]] = []  # (file, start_pos, end_pos)
    headers = list(_DIFF_FILE_HEADER.finditer(diff_text))
    for idx, header in enumerate(headers):
        file_path = header.group(1)
        region_start = header.start()
        region_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(diff_text)
        file_regions.append((file_path, region_start, region_end))

    for pattern_name, pattern, severity, description, suggestion in _SECURITY_PATTERNS:
        for match in pattern.finditer(diff_text):
            pos = match.start()
            # Determine which file region this match belongs to
            file_path = "(unknown)"
            for fpath, rstart, rend in file_regions:
                if rstart <= pos < rend:
                    file_path = fpath
                    break

            line_range, matched_text = _extract_context(diff_text, pattern, match)

            results.append(
                SecurityReviewResult(
                    file=file_path,
                    severity=severity,
                    description=description,
                    line_range=line_range,
                    suggestion=suggestion,
                    pattern_name=pattern_name,
                    matched_text=matched_text,
                )
            )

    # Sort: critical > high > medium > low, then by file name
    severity_order: dict[Severity, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    results.sort(key=lambda r: (severity_order.get(r.severity, 99), r.file))

    return results


def format_security_review(results: list[SecurityReviewResult]) -> str:
    """Format security review results as a Rich-compatible string.

    Args:
        results: List of SecurityReviewResult from run_security_review.

    Returns:
        Formatted string with severity-coloured headings and findings.
    """
    if not results:
        return "[green]\u2713 Security review passed: no issues detected[/green]"

    severity_icons: dict[Severity, str] = {
        "critical": "\U0001f534",  # red circle
        "high": "\U0001f7e0",  # orange circle
        "medium": "\U0001f7e1",  # yellow circle
        "low": "\U0001f535",  # blue circle
    }
    severity_colours: dict[Severity, str] = {
        "critical": "red",
        "high": "dark_orange",
        "medium": "yellow",
        "low": "blue",
    }

    lines: list[str] = []
    lines.append(f"[bold red]\u2718 Security review: {len(results)} issue(s) found[/bold red]")
    lines.append("")

    by_file: dict[str, list[SecurityReviewResult]] = {}
    for r in results:
        by_file.setdefault(r.file, []).append(r)

    for file_path, file_results in sorted(by_file.items()):
        lines.append(f"[bold underline]{file_path}[/bold underline]")
        for r in file_results:
            icon = severity_icons.get(r.severity, "?")
            colour = severity_colours.get(r.severity, "white")
            line_info = ""
            if r.line_range:
                start, end = r.line_range
                line_info = f" (line {start}" + (f"-{end}" if end != start else "") + ")"
            lines.append(f"  [{colour}]{icon} [{r.severity.upper()}][/{colour}]{line_info} {r.description}")
            if r.suggestion:
                lines.append(f"   [dim]-> {r.suggestion}[/dim]")
        lines.append("")

    # Summary
    counts: dict[Severity, int] = {}
    for r in results:
        counts[r.severity] = counts.get(r.severity, 0) + 1

    lines.append("[bold]Summary:[/bold]")
    for sev in ("critical", "high", "medium", "low"):
        count = counts.get(sev, 0)
        if count > 0:
            colour = severity_colours[sev]
            lines.append(f"  [{colour}]{sev.upper()}: {count}[/{colour}]")

    return "\n".join(lines)


@dataclass(frozen=True)
class SecurityReviewSummary:
    """Compact summary of a security review run.

    Attributes:
        total_findings: Total number of findings.
        by_severity: Count of findings per severity level.
        blocked: True if any critical or high findings exist.
        results: Full list of SecurityReviewResult.
    """

    total_findings: int
    by_severity: dict[Severity, int]
    blocked: bool
    results: list[SecurityReviewResult] = field(repr=False)


def summarize_security_review(results: list[SecurityReviewResult]) -> SecurityReviewSummary:
    """Produce a compact summary from security review results.

    Args:
        results: List of SecurityReviewResult.

    Returns:
        SecurityReviewSummary with counts and a blocked flag.
    """
    by_severity: dict[Severity, int] = {}
    for r in results:
        by_severity[r.severity] = by_severity.get(r.severity, 0) + 1

    return SecurityReviewSummary(
        total_findings=len(results),
        by_severity=by_severity,
        blocked=by_severity.get("critical", 0) > 0 or by_severity.get("high", 0) > 0,
        results=results,
    )
