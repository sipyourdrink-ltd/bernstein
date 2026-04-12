"""Automated quality gates: lint, type-check, test, mutation, and intent verification gates.

Runs configurable code quality checks after a task agent finishes but before
the approval gate evaluates the work. Hard-blocks merge when enabled gates fail.
Records results to .sdd/metrics/quality_gates.jsonl for trend analysis.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.defaults import GATE
from bernstein.core.telemetry import start_span

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.gate_runner import GatePipelineStep, GateReport
    from bernstein.core.models import Task
    from bernstein.core.quality_score import QualityScore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent verification constants
# ---------------------------------------------------------------------------

_INTENT_MAX_DIFF_CHARS = GATE.intent_max_diff_chars
_INTENT_MAX_TOKENS = GATE.intent_max_tokens
_INTENT_DEFAULT_MODEL = "google/gemini-flash-1.5"
_INTENT_PROVIDER = "openrouter"

#: Maximum characters from the parent agent's context to prepend when forking.
_FORK_CONTEXT_MAX_CHARS = GATE.fork_context_max_chars

_INTENT_PROMPT_TEMPLATE = """\
You are an intent verifier. A task was given to an AI agent. Compare the \
original task description with what the agent actually produced.

## Original Task
**Title:** {title}
**Description:**
{description}

## Agent Output (git diff)
```diff
{diff}
```

## Agent's Result Summary
{result_summary}

## Instructions
Determine whether the agent's output satisfies the original task's intent.

- **yes**: The output clearly addresses what was asked. Minor deviations are fine.
- **partially**: The output addresses some of what was asked but misses key \
requirements or diverges in scope.
- **no**: The output does not satisfy the task intent. It either did the wrong \
thing or failed to address the core requirement.

Output a JSON object with exactly these fields:
{{
  "verdict": "yes | partially | no",
  "reason": "One sentence explaining the verdict"
}}

Output ONLY the JSON. No markdown fences. No extra text.
"""


@dataclass(frozen=True)
class IntentVerificationConfig:
    """Configuration for the intent verification quality gate.

    Asks a cheap LLM: "Task asked for X. Agent produced Y. Does Y satisfy X?"
    and blocks merge when the verdict is "no" (and optionally "partially").

    Attributes:
        enabled: Master switch — when False, the gate does not run.
        model: OpenRouter model for verification (cheap model recommended).
        provider: LLM provider key passed to call_llm.
        max_diff_chars: Truncate diff at this length for cost control.
        max_tokens: Token cap for the LLM response.
        block_on_no: Block merge when verdict is "no" (default True).
        block_on_partial: Block merge when verdict is "partially" (default False).
        fork_from_context: Optional context prefix from the completed agent's
            session. When set, the gate is "forked" from the parent agent's
            context: the prefix is prepended to the verification prompt so that
            both share the same prompt prefix and benefit from KV-cache reuse.
            Typical value is the agent's final system-prompt or conversation
            summary. Limited to ``_FORK_CONTEXT_MAX_CHARS`` characters.
    """

    enabled: bool = False
    model: str = _INTENT_DEFAULT_MODEL
    provider: str = _INTENT_PROVIDER
    max_diff_chars: int = _INTENT_MAX_DIFF_CHARS
    max_tokens: int = _INTENT_MAX_TOKENS
    block_on_no: bool = True
    block_on_partial: bool = False
    fork_from_context: str | None = None


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for the benchmark regression quality gate.

    Attributes:
        enabled: Whether to run the benchmark regression gate.
        command: Shell command that runs benchmarks and writes results to
            ``.benchmark_results.json`` (pytest-benchmark JSON format).
        threshold: Maximum allowed regression ratio (0.0-1.0). A value of
            0.10 means a 10% degradation in response time, throughput, or
            memory blocks merge.
    """

    enabled: bool = False
    command: str = "uv run pytest benchmarks/ --benchmark-json=.benchmark_results.json -q"
    threshold: float = 0.10


@dataclass(frozen=True)
class QualityGatesConfig:
    """Configuration for automated quality gates.

    Attributes:
        enabled: Master switch — when False, no gates run.
        lint: Run lint gate (ruff check by default).
        lint_command: Shell command for linting.
        type_check: Run type-check gate (pyright by default).
        type_check_command: Shell command for type checking.
        tests: Run test gate.
        test_command: Shell command for running tests.
        timeout_s: Per-gate command timeout in seconds.
        pipeline: Optional explicit gate pipeline. When omitted, a default
            pipeline is synthesized from the legacy booleans.
        allow_bypass: Whether CLI-driven gate bypass is allowed.
        cache_enabled: Whether gate results can be reused from cache.
        base_ref: Git base ref used for incremental diff fallback.
        mutation_testing: Run mutation testing gate (mutmut by default).
        mutation_command: Shell command that runs mutation tests and prints results.
        mutation_threshold: Minimum required mutation score (0.0-1.0). Blocks if below.
        mutation_timeout_s: Timeout for mutation testing (longer than other gates).
        intent_verification: Config for the LLM-based intent verification gate.
        benchmark: Config for the performance benchmark regression gate.
        auto_format: Run automatic code formatting before lint. Auto-fixes
            formatting issues on changed files rather than blocking merge.
        auto_format_python_command: Shell command for Python formatting
            (applied to changed .py files; default: ``ruff format``).
        auto_format_js_command: Shell command for JS/TS formatting
            (applied to changed .js/.ts/.jsx/.tsx files; default: ``prettier --write``).
        auto_format_rust_command: Shell command for Rust formatting
            (applied to changed .rs files; default: ``rustfmt``).
        dead_code_check_lost_callers: When True, the dead-code gate also scans
            the entire codebase for callers of names removed in the diff.
        dead_code_check_unused_imports: When True, check for unused imports
            via AST analysis (in addition to vulture).
        dead_code_check_unreachable: When True, detect unreachable branches
            via AST pattern matching.
        comment_quality_check: Run comment quality gate on changed Python files.
            Checks docstring accuracy, completeness, redundancy, and style.
        comment_quality_docstyle: Expected docstring style for the comment-quality
            gate. One of ``"google"``, ``"numpy"``, ``"rest"``, or ``"auto"``
            (auto-detect per docstring).
    """

    enabled: bool = True
    lint: bool = True
    lint_command: str = "ruff check ."
    type_check: bool = False
    type_check_command: str = "pyright"
    tests: bool = False
    test_command: str = "uv run python scripts/run_tests.py -x"
    timeout_s: int = 120
    pipeline: list[GatePipelineStep] | None = None
    allow_bypass: bool = False
    cache_enabled: bool = True
    base_ref: str = "main"
    mutation_testing: bool = False
    mutation_command: str = "uv run mutmut run"
    mutation_threshold: float = 0.50
    mutation_timeout_s: int = 600
    intent_verification: IntentVerificationConfig = field(default_factory=IntentVerificationConfig)
    pii_scan: bool = True
    pii_scan_paths: list[str] = field(default_factory=lambda: ["src/"])
    pii_ignore_paths: list[str] = field(default_factory=list[str])
    pii_allowlist_prefixes: list[str] = field(
        default_factory=lambda: ["FAKE", "TEST", "EXAMPLE", "DUMMY", "PLACEHOLDER", "LOCALHOST"]
    )
    security_scan: bool = False
    security_scan_command: str | None = None
    coverage_delta: bool = False
    coverage_delta_command: str | None = None
    complexity_check: bool = False
    complexity_threshold: float = 0.20
    complexity_check_command: str | None = None
    dead_code_check: bool = False
    dead_code_command: str = "vulture"
    dead_code_min_confidence: int = 80
    dead_code_check_lost_callers: bool = True
    dead_code_check_unused_imports: bool = True
    dead_code_check_unreachable: bool = True
    comment_quality_check: bool = False
    comment_quality_docstyle: str = "auto"
    import_cycle_check: bool = False
    import_cycle_command: str | None = None
    merge_conflict_check: bool = False
    flaky_detection: bool = False
    flaky_min_runs: int = 5
    flaky_threshold: float = 0.15
    dep_audit: bool = False
    dep_audit_command: str = "pip-audit"
    dep_audit_files: list[str] = field(
        default_factory=lambda: [
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "uv.lock",
        ]
    )
    benchmark: BenchmarkConfig = field(default_factory=BenchmarkConfig)
    migration_reversibility_check: bool = False
    large_file_check: bool = False
    large_file_threshold: int = 500
    integration_test_gen: bool = False
    review_rubric: bool = False
    dlp_scan: bool = True
    dlp_check_license_violations: bool = True
    dlp_check_regulated_data: bool = True
    dlp_check_proprietary_data: bool = True
    dlp_block_license_violations: bool = True
    dlp_block_regulated_data: bool = True
    dlp_block_proprietary_data: bool = False
    dlp_internal_url_patterns: list[str] = field(default_factory=list)
    dlp_ignore_paths: list[str] = field(default_factory=list)
    dlp_allowlist_prefixes: list[str] = field(
        default_factory=lambda: ["FAKE", "TEST", "EXAMPLE", "DUMMY", "PLACEHOLDER", "MOCK", "SAMPLE"]
    )
    auto_format: bool = False
    auto_format_python_command: str = "ruff format"
    auto_format_js_command: str = "prettier --write"
    auto_format_rust_command: str = "rustfmt"
    test_expansion: bool = False
    agent_test_mutation: bool = False
    agent_test_mutation_threshold: float = 0.70
    agent_test_mutation_timeout_s: int = 300


@dataclass
class QualityGateCheckResult:
    """Result of a single quality gate check.

    Attributes:
        gate: Gate name (e.g. "lint", "type_check", "tests").
        passed: Whether the check passed.
        blocked: True if this is a hard block (merge must not proceed).
        detail: Human-readable description of findings (truncated at 2000 chars).
    """

    gate: str
    passed: bool
    blocked: bool
    detail: str
    status: str = "pass"


@dataclass
class QualityGatesResult:
    """Overall result of all quality gate checks for a task.

    Attributes:
        task_id: ID of the task checked.
        passed: True if all blocking gates passed (or no gates ran).
        gate_results: Per-gate results in run order.
    """

    task_id: str
    passed: bool
    gate_results: list[QualityGateCheckResult] = field(default_factory=list[QualityGateCheckResult])
    quality_score: QualityScore | None = None


# ---------------------------------------------------------------------------
# Intent verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentVerdict:
    """Result of an LLM-based intent verification check.

    Attributes:
        verdict: "yes", "partially", or "no".
        reason: One-sentence explanation from the LLM.
        model: Model that performed the check.
    """

    verdict: Literal["yes", "partially", "no"]
    reason: str
    model: str = ""


def _get_intent_diff(worktree_path: Path, owned_files: list[str]) -> str:
    """Return the git diff for intent verification (HEAD~1 or staged)."""
    try:
        cmd = ["git", "diff", "HEAD~1", "--"]
        if owned_files:
            cmd.extend(owned_files)
        result = subprocess.run(cmd, cwd=worktree_path, capture_output=True, text=True, timeout=30)
        diff = result.stdout.strip()
        if not diff:
            result = subprocess.run(
                ["git", "diff", "HEAD", "--"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            diff = result.stdout.strip()
        return diff or "(no diff available)"
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("intent_verification: git diff failed: %s", exc)
        return "(failed to get git diff)"


def _parse_intent_response(raw: str, model: str) -> IntentVerdict:
    """Parse the LLM response into an IntentVerdict.

    Defaults to "yes" when the response cannot be parsed so a model outage
    never permanently blocks the pipeline.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()

    data: dict[str, object] = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(text[start:end])

    if not data:
        logger.warning("intent_verification: unparseable response — defaulting to yes: %.200s", text)
        return IntentVerdict(
            verdict="yes",
            reason="Verifier returned unparseable response — defaulting to yes",
            model=model,
        )

    raw_verdict = str(data.get("verdict", "yes")).lower().strip()
    verdict: Literal["yes", "partially", "no"]
    if raw_verdict == "no":
        verdict = "no"
    elif raw_verdict == "partially":
        verdict = "partially"
    else:
        verdict = "yes"

    return IntentVerdict(
        verdict=verdict,
        reason=str(data.get("reason", "")),
        model=model,
    )


async def _verify_intent_async(task: Task, worktree_path: Path, config: IntentVerificationConfig) -> IntentVerdict:
    """Async core for intent verification — call the LLM and return a verdict."""
    from bernstein.core.llm import call_llm

    diff = _get_intent_diff(worktree_path, task.owned_files)
    if len(diff) > config.max_diff_chars:
        diff = diff[: config.max_diff_chars] + "\n... (truncated)"

    result_summary = task.result_summary or "(no result summary provided)"

    body = _INTENT_PROMPT_TEMPLATE.format(
        title=task.title,
        description=task.description[:2000],
        diff=diff,
        result_summary=result_summary[:500],
    )

    if config.fork_from_context:
        # Forked-gate pattern: prepend the parent agent's context so both the
        # agent's original session and this verification gate share the same
        # prompt prefix, enabling KV-cache reuse on providers that support it.
        ctx = config.fork_from_context[:_FORK_CONTEXT_MAX_CHARS]
        prompt = f"## Agent Session Context\n{ctx}\n\n---\n\n{body}"
    else:
        prompt = body

    logger.info(
        "intent_verification: task=%s model=%s diff_chars=%d fork=%s",
        task.id,
        config.model,
        len(diff),
        config.fork_from_context is not None,
    )

    try:
        raw = await call_llm(
            prompt=prompt,
            model=config.model,
            provider=config.provider,
            max_tokens=config.max_tokens,
            temperature=0.0,
        )
    except RuntimeError as exc:
        logger.warning(
            "intent_verification: LLM call failed for task %s: %s — defaulting to yes",
            task.id,
            exc,
        )
        return IntentVerdict(
            verdict="yes",
            reason=f"Verifier call failed: {exc} — defaulting to yes",
            model=config.model,
        )

    result = _parse_intent_response(raw, config.model)
    logger.info("intent_verification: task=%s verdict=%s reason=%s", task.id, result.verdict, result.reason)
    return result


def _run_intent_gate(
    task: Task,
    worktree_path: Path,
    config: IntentVerificationConfig,
) -> tuple[IntentVerdict, bool]:
    """Run intent verification synchronously; return (verdict, blocked).

    Args:
        task: The completed task.
        worktree_path: Path to the agent worktree for git diff.
        config: Intent verification configuration.

    Returns:
        Tuple of (IntentVerdict, blocked_bool).
    """
    verdict = asyncio.run(_verify_intent_async(task, worktree_path, config))
    blocked = (verdict.verdict == "no" and config.block_on_no) or (
        verdict.verdict == "partially" and config.block_on_partial
    )
    return verdict, blocked


def run_intent_gate_sync(
    task: Task,
    worktree_path: Path,
    config: IntentVerificationConfig,
) -> tuple[IntentVerdict, bool]:
    """Public sync wrapper used by the async gate runner."""
    return _run_intent_gate(task, worktree_path, config)


# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------


def _run_command(command: str, cwd: Path, timeout_s: int) -> tuple[bool, str]:
    """Run a shell command and return (success, output).

    Args:
        command: Shell command to run.
        cwd: Working directory for the subprocess.
        timeout_s: Timeout in seconds before the process is killed.

    Returns:
        Tuple of (exit_code_zero, combined_stdout_stderr_output).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,  # SECURITY: shell=True required because quality gate commands
            # are admin-configured shell strings (e.g. "ruff check src/")
            # that may use pipes or globs; not user input
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        output = (proc.stdout + proc.stderr).strip()
        if len(output) > 2000:
            output = output[:2000] + "\n... (truncated)"
        return proc.returncode == 0, output or "(no output)"
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout_s}s"
    except OSError as exc:
        return False, f"Command error: {exc}"


def run_command_sync(command: str, cwd: Path, timeout_s: int) -> tuple[bool, str]:
    """Public sync wrapper used by the async gate runner."""
    return _run_command(command, cwd, timeout_s)


# ---------------------------------------------------------------------------
# Mutation testing helpers
# ---------------------------------------------------------------------------


def _parse_mutation_score(output: str) -> float | None:
    """Extract a mutation score (0.0-1.0) from mutation tool output.

    Supports output formats from mutmut and mutatest. Returns None when the
    output cannot be parsed (caller should fall back to exit-code semantics).

    Args:
        output: Combined stdout/stderr from the mutation testing command.

    Returns:
        Float mutation score in [0.0, 1.0], or None if unparseable.
    """
    # mutmut run: "🎉 42/100  🤔 0  🙁 58  🔇 0"
    # The first "killed/total" fraction appears as digits-slash-digits.
    m = re.search(r"\b(\d+)/(\d+)\b", output)
    if m:
        killed, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            return killed / total
        return None  # zero mutants — can't produce a meaningful score

    # mutatest / generic:
    #   "Killed: 42\nSurvived: 58"   (keyword before number)
    #   "42 killed, 58 survived"      (number before keyword)
    # Use [ \t]+ (horizontal whitespace only) for the N-before-keyword form so
    # a newline between a digit and the next keyword on a new line is not matched.
    killed_m = re.search(r"(?:[Kk]illed[: \t]+(\d+)|(\d+)[ \t]+[Kk]illed)", output)
    survived_m = re.search(r"(?:[Ss]urvived[: \t]+(\d+)|(\d+)[ \t]+[Ss]urvived)", output)
    if killed_m and survived_m:
        killed = int(next(g for g in killed_m.groups() if g is not None))
        survived = int(next(g for g in survived_m.groups() if g is not None))
        total = killed + survived
        if total > 0:
            return killed / total
        return None

    return None


def _run_mutation_gate(config: QualityGatesConfig, run_dir: Path) -> tuple[bool, str, float | None]:
    """Run mutation testing and compare score against the configured threshold.

    Args:
        config: Quality gates configuration.
        run_dir: Directory to run the mutation command in.

    Returns:
        Tuple of (passed, detail_message, score_or_None).
        ``passed`` is True when the mutation score meets the threshold.
        ``score_or_None`` is the parsed float score, or None when unparseable.
    """
    _ok, output = _run_command(config.mutation_command, run_dir, config.mutation_timeout_s)

    score = _parse_mutation_score(output)

    if score is not None:
        passed = score >= config.mutation_threshold
        status = "\u2265" if passed else "<"
        detail = f"Mutation score: {score:.1%} ({status} threshold {config.mutation_threshold:.1%})\n{output}"
        return passed, detail, score

    # Could not parse a numeric score — treat non-zero exit as failure.
    passed = _ok
    detail = (
        f"Could not parse mutation score (threshold {config.mutation_threshold:.1%}). "
        f"Exit: {'0' if _ok else 'non-zero'}\n{output}"
    )
    return passed, detail, None


def run_mutation_gate_sync(config: QualityGatesConfig, run_dir: Path) -> tuple[bool, str, float | None]:
    """Public sync wrapper used by the async gate runner."""
    return _run_mutation_gate(config, run_dir)


# ---------------------------------------------------------------------------
# Agent-written test mutation verification (ROAD-170)
# ---------------------------------------------------------------------------

_TEST_FILE_PATTERN = re.compile(r"^\+\+\+ b/(tests?/\S*test_\w+\.py|\w+/tests?/test_\w+\.py)", re.MULTILINE)
_SOURCE_FROM_TEST = re.compile(r"test_(\w+)\.py$")


def _extract_agent_test_files(diff: str) -> list[str]:
    """Return relative paths of test files added or modified in the diff.

    Args:
        diff: Raw git diff output.

    Returns:
        List of test file paths (relative to repo root).
    """
    return [m.group(1) for m in _TEST_FILE_PATTERN.finditer(diff)]


def _infer_source_files(test_files: list[str], run_dir: Path) -> list[str]:
    """Guess the production source file for each test file.

    Converts ``tests/unit/test_foo.py`` → ``src/bernstein/core/foo.py`` by
    searching the source tree for a matching filename.

    Args:
        test_files: Test file paths relative to repo root.
        run_dir: Repo root directory used for filesystem searches.

    Returns:
        De-duplicated list of inferred source file paths that actually exist.
    """
    import glob
    from pathlib import Path as _Path

    source_files: list[str] = []
    run_dir_path = _Path(run_dir)
    for test_path in test_files:
        m = _SOURCE_FROM_TEST.search(test_path)
        if not m:
            continue
        module_stem = m.group(1)
        # Search for src/**/<module_stem>.py
        pattern = str(run_dir_path / "src" / "**" / f"{module_stem}.py")
        matches = glob.glob(pattern, recursive=True)
        for match in matches:
            rel = str(_Path(match).relative_to(run_dir_path))
            if rel not in source_files:
                source_files.append(rel)
    return source_files


def _build_agent_mutation_command(source_files: list[str], test_files: list[str]) -> str:
    """Build a targeted mutmut command for specific source+test files.

    Args:
        source_files: Source files to mutate.
        test_files: Test files to run against mutants.

    Returns:
        Shell command string suitable for passing to ``_run_command()``.
    """
    paths = " ".join(source_files)
    tests = " ".join(test_files)
    return (
        f"uv run mutmut run --paths-to-mutate {paths} "
        f"--test-command 'python -m pytest {tests} -x -q --no-header --override-ini=addopts='"
    )


def run_agent_test_mutation_gate_sync(
    config: QualityGatesConfig,
    task: Task,
    run_dir: Path,
) -> tuple[bool, str, float | None]:
    """Verify that agent-written tests actually catch bugs via targeted mutation testing.

    Extracts test files from the agent's git diff, infers the corresponding
    source files, and runs mutmut targeted at just those files.  Returns a
    failure result if the mutation score falls below
    ``config.agent_test_mutation_threshold``.

    Different from the general ``mutation_testing`` gate (TEST-015 / ROAD-018),
    which runs full mutation testing on Bernstein's own test suite.  This gate
    runs *per agent task*, focused only on the tests the agent produced.

    Args:
        config: Quality gates configuration.
        task: The completed agent task (used to get owned files for diff).
        run_dir: Repository root for running commands.

    Returns:
        Tuple of (passed, detail_message, score_or_None).
    """
    diff = _get_intent_diff(run_dir, task.owned_files or [])
    test_files = _extract_agent_test_files(diff)
    if not test_files:
        return True, "No agent-written test files detected in diff — skipping agent mutation gate.", None

    source_files = _infer_source_files(test_files, run_dir)
    if not source_files:
        return (
            True,
            f"Could not infer source files for tests: {test_files} — skipping agent mutation gate.",
            None,
        )

    command = _build_agent_mutation_command(source_files, test_files)
    _ok, output = _run_command(command, run_dir, config.agent_test_mutation_timeout_s)

    score = _parse_mutation_score(output)
    threshold = config.agent_test_mutation_threshold

    if score is not None:
        passed = score >= threshold
        status = "\u2265" if passed else "<"
        detail = (
            f"Agent test mutation score: {score:.1%} ({status} threshold {threshold:.1%})\n"
            f"Test files: {', '.join(test_files)}\n"
            f"Source files: {', '.join(source_files)}\n{output}"
        )
        return passed, detail, score

    # Could not parse — fall back to exit code
    passed = _ok
    detail = (
        f"Could not parse agent mutation score (threshold {threshold:.1%}). "
        f"Exit: {'0' if _ok else 'non-zero'}\n"
        f"Test files: {', '.join(test_files)}\n"
        f"Source files: {', '.join(source_files)}\n{output}"
    )
    return passed, detail, None


# ---------------------------------------------------------------------------
# PII / secret scan gate
# ---------------------------------------------------------------------------


def _run_pii_gate(
    config: QualityGatesConfig,
    run_dir: Path,
    changed_files: list[str] | None = None,
) -> QualityGateCheckResult:
    """Scan files in configured paths or the changed-file set for secrets and PII.

    Imports ``pii_output_gate`` and scans each file under ``config.pii_scan_paths``
    for leaked secrets.  Any high-severity finding blocks merge.

    Args:
        config: Quality gates configuration (uses ``pii_scan_paths``).
        run_dir: Working directory (agent worktree root).
        changed_files: Optional changed-file set. When provided, only these
            files are scanned.

    Returns:
        QualityGateCheckResult with ``blocked=True`` if any high-severity
        finding is detected.
    """
    from pathlib import Path as _Path

    from bernstein.core.pii_output_gate import format_findings, scan_text

    all_findings: list[Any] = []
    scan_targets: list[_Path]
    if changed_files is not None:
        scan_targets = [(_Path(run_dir) / rel_path) for rel_path in changed_files]
    else:
        scan_targets = []
        for scan_path in config.pii_scan_paths:
            target = _Path(run_dir) / scan_path
            if not target.exists():
                continue
            scan_targets.extend([target] if target.is_file() else sorted(target.rglob("*")))

    for fpath in scan_targets:
        if not fpath.is_file():
            continue
        # Skip binary / non-text files
        _skip = {
            ".pyc",
            ".pyo",
            ".so",
            ".dylib",
            ".whl",
            ".egg",
            ".gz",
            ".zip",
            ".tar",
            ".png",
            ".jpg",
            ".gif",
            ".ico",
            ".pdf",
        }
        if fpath.suffix in _skip:
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        findings = scan_text(
            content,
            path=fpath.relative_to(run_dir).as_posix(),
            ignore_paths=config.pii_ignore_paths,
            allowlist_prefixes=config.pii_allowlist_prefixes,
        )
        for f in findings:
            # Annotate finding with file path for the report
            all_findings.append((str(fpath.relative_to(run_dir)), f))

    if not all_findings:
        return QualityGateCheckResult(
            gate="pii_scan",
            passed=True,
            blocked=False,
            detail="No secrets or PII detected in agent output.",
        )

    # Any high-severity finding blocks merge
    has_high = any(f.severity == "high" for _, f in all_findings)
    finding_objs = [f for _, f in all_findings]
    detail = format_findings(finding_objs)

    # Prepend file paths to detail
    file_lines: list[str] = []
    for fpath_str, f in all_findings:
        file_lines.append(f"  {fpath_str}:{f.line_number} [{f.severity.upper()}] {f.rule}")
    detail = detail + "\n\nFiles:\n" + "\n".join(file_lines)

    if len(detail) > 2000:
        detail = detail[:2000] + "\n... (truncated)"

    return QualityGateCheckResult(
        gate="pii_scan",
        passed=not has_high,
        blocked=has_high,
        detail=detail,
    )


def run_pii_gate_sync(
    config: QualityGatesConfig,
    run_dir: Path,
    changed_files: list[str] | None = None,
) -> QualityGateCheckResult:
    """Public sync wrapper used by the async gate runner."""
    return _run_pii_gate(config, run_dir, changed_files)


# ---------------------------------------------------------------------------
# DLP scan gate
# ---------------------------------------------------------------------------


def _run_dlp_gate(
    config: QualityGatesConfig,
    run_dir: Path,
    changed_files: list[str] | None = None,
) -> QualityGateCheckResult:
    """Scan files for DLP violations: license issues, regulated data, and proprietary patterns.

    Extends the PII gate with additional categories:
    - License violations (third-party copyright headers, SPDX identifiers)
    - Regulated data (PHI: NPI numbers, ICD-10 codes, MRNs, DEA numbers)
    - Proprietary data (internal hostnames, customer IDs, RFC-1918 addresses)

    Hard-blocks merge when ``dlp_block_license_violations`` or
    ``dlp_block_regulated_data`` are True and matching findings are detected.

    Args:
        config: Quality gates configuration.
        run_dir: Working directory (agent worktree root).
        changed_files: Optional changed-file set. When provided, only these
            files are scanned.

    Returns:
        QualityGateCheckResult with ``blocked=True`` if any hard-block finding
        is detected.
    """
    from pathlib import Path as _Path

    from bernstein.core.dlp_scanner import DLPConfig, DLPScanner

    dlp_config = DLPConfig(
        enabled=True,
        check_license_violations=config.dlp_check_license_violations,
        check_regulated_data=config.dlp_check_regulated_data,
        check_proprietary_data=config.dlp_check_proprietary_data,
        block_license_violations=config.dlp_block_license_violations,
        block_regulated_data=config.dlp_block_regulated_data,
        block_proprietary_data=config.dlp_block_proprietary_data,
        internal_url_patterns=list(config.dlp_internal_url_patterns),
        ignore_paths=list(config.dlp_ignore_paths),
        allowlist_prefixes=list(config.dlp_allowlist_prefixes),
    )
    scanner = DLPScanner(dlp_config)

    _skip_extensions = {
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".whl",
        ".egg",
        ".gz",
        ".zip",
        ".tar",
        ".png",
        ".jpg",
        ".gif",
        ".ico",
        ".pdf",
    }
    # Files that *define* the DLP / PII patterns themselves will always
    # match their own rules ("All rights reserved", credit-card regexes, etc.)
    # — scanning them produces guaranteed false positives that block legit
    # merges (incident 2026-04-11: architect-80d4691e blocked on the very
    # commit that *added* DLP rules to dlp_scanner.py).
    _self_skip_paths = (
        "src/bernstein/core/dlp_scanner.py",
        "src/bernstein/core/pii_output_gate.py",
        "src/bernstein/core/sensitive_file_detector.py",
        "src/bernstein/core/quality_gates.py",
        "tests/unit/test_dlp_scanner.py",
        "tests/unit/test_pii_output_gate.py",
        "tests/unit/test_sensitive_file_detector.py",
        "tests/unit/test_quality_gates.py",
    )

    scan_targets: list[_Path]
    if changed_files is not None:
        scan_targets = [_Path(run_dir) / rel for rel in changed_files]
    else:
        scan_targets = sorted(_Path(run_dir).rglob("*"))

    all_findings: list[tuple[str, Any]] = []

    for fpath in scan_targets:
        if not fpath.is_file():
            continue
        if fpath.suffix in _skip_extensions:
            continue
        # Check ignore paths
        rel = str(fpath.relative_to(run_dir))
        if rel in _self_skip_paths:
            continue
        if any(rel.startswith(ig.rstrip("/")) or fnmatch(rel, ig) for ig in config.dlp_ignore_paths):
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        result = scanner.scan_text(content)
        for finding in result.findings:
            all_findings.append((rel, finding))

    if not all_findings:
        return QualityGateCheckResult(
            gate="dlp_scan",
            passed=True,
            blocked=False,
            detail="DLP scan: no violations detected.",
        )

    has_blocks = any(f.block_merge for _, f in all_findings)
    categories = sorted({f.category for _, f in all_findings})
    lines = [f"DLP scan: {len(all_findings)} finding(s) — categories: {', '.join(categories)}"]
    for fpath_str, f in all_findings:
        block_label = "[BLOCK]" if f.block_merge else "[WARN]"
        lines.append(
            f"  {block_label} [{f.severity.upper()}] {fpath_str}"
            f" (line {f.line_number}): {f.description} — {f.redacted_match}"
        )
    detail = "\n".join(lines)
    if len(detail) > 2000:
        detail = detail[:2000] + "\n... (truncated)"

    return QualityGateCheckResult(
        gate="dlp_scan",
        passed=not has_blocks,
        blocked=has_blocks,
        detail=detail,
    )


def run_dlp_gate_sync(
    config: QualityGatesConfig,
    run_dir: Path,
    changed_files: list[str] | None = None,
) -> QualityGateCheckResult:
    """Public sync wrapper used by the async gate runner."""
    return _run_dlp_gate(config, run_dir, changed_files)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_quality_gates(
    task: Task,
    run_dir: Path,
    workdir: Path,
    config: QualityGatesConfig,
    *,
    skip_gates: list[str] | None = None,
    bypass_reason: str | None = None,
) -> QualityGatesResult:
    """Run all enabled quality gates on a completed task's changes.

    Args:
        task: The completed task being validated.
        run_dir: Directory to run gate commands in (agent worktree or workdir).
        workdir: Project root for writing metrics to .sdd/metrics/.
        config: Which gates to run and their command/timeout configuration.
        skip_gates: Optional gate names to bypass for this run.
        bypass_reason: Optional human-readable bypass reason.

    Returns:
        QualityGatesResult with per-gate outcomes and overall passed flag.
    """
    from bernstein.core.gate_runner import GateRunner

    if not config.enabled:
        return QualityGatesResult(task_id=task.id, passed=True)

    explicit_skip_gates = skip_gates if skip_gates is not None else _env_skip_gates()
    explicit_bypass_reason = bypass_reason if bypass_reason is not None else _env_bypass_reason()
    runner = GateRunner(config, workdir, base_ref=config.base_ref)

    with start_span("task.verify", {"task.id": task.id}):
        report = asyncio.run(
            runner.run_all(
                task,
                run_dir,
                skip_gates=explicit_skip_gates,
                bypass_reason=explicit_bypass_reason,
            )
        )
    quality_score = None
    try:
        from bernstein.core.quality_score import QualityScorer

        scorer = QualityScorer(workdir)
        quality_score = scorer.score(report)
        scorer.record(task.id, quality_score)
    except Exception as exc:  # pragma: no cover - best-effort telemetry only
        logger.warning("Failed to record quality score for task %s: %s", task.id, exc)

    result = _legacy_result_from_report(report, quality_score=quality_score)
    for gate_result in report.results:
        _record_gate_event(
            task.id,
            gate_result.name,
            _result_str_from_status(gate_result.status, gate_result.blocked),
            workdir,
            status=gate_result.status,
            duration_ms=gate_result.duration_ms,
            cached=gate_result.cached,
            required=gate_result.required,
            extra=gate_result.metadata or None,
        )
        if gate_result.blocked:
            logger.warning(
                "Quality gate [%s] blocked task %s: %s",
                gate_result.name,
                task.id,
                gate_result.details[:200],
            )
    return result


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


def _result_str(check: QualityGateCheckResult) -> str:
    """Translate a QualityGateCheckResult to a metrics result string."""
    if check.passed:
        return "pass"
    if check.blocked:
        return "blocked"
    return "flagged"


def _result_str_from_status(status: str, blocked: bool) -> str:
    """Translate a detailed gate status to the legacy metrics result string."""
    if status in {"pass", "skipped"}:
        return "pass"
    if blocked:
        return "blocked"
    return "flagged"


def _record_gate_event(
    task_id: str,
    gate: str,
    result: str,
    workdir: Path,
    *,
    status: str | None = None,
    duration_ms: int | None = None,
    cached: bool | None = None,
    required: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a quality gate event to .sdd/metrics/quality_gates.jsonl.

    Args:
        task_id: ID of the task being checked.
        gate: Gate name (e.g. "lint").
        result: Outcome string: "pass", "blocked", or "flagged".
        workdir: Project root directory.
        extra: Optional extra fields merged into the event (e.g. mutation_score).
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "gate": gate,
        "result": result,
    }
    if status is not None:
        event["status"] = status
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    if cached is not None:
        event["cached"] = cached
    if required is not None:
        event["required"] = required
    if extra:
        event.update(extra)
    try:
        with open(metrics_dir / "quality_gates.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.debug("Could not write quality gate event: %s", exc)


def get_quality_gate_stats(workdir: Path) -> dict[str, Any]:
    """Read .sdd/metrics/quality_gates.jsonl and return aggregate stats.

    Returns a dict with:
      - total: total events recorded
      - blocked: events with result "blocked"
      - by_gate: per-gate breakdown {gate: {pass: N, blocked: N}}

    Args:
        workdir: Project root directory.
    """
    metrics_file = workdir / ".sdd" / "metrics" / "quality_gates.jsonl"
    if not metrics_file.exists():
        return {"total": 0, "blocked": 0, "by_gate": {}}

    total = blocked = 0
    by_gate: dict[str, dict[str, int]] = {}

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        gate = str(event.get("gate", "unknown"))
        result_val = str(event.get("result", "pass"))
        total += 1
        if result_val == "blocked":
            blocked += 1

        counts = by_gate.setdefault(gate, {"pass": 0, "blocked": 0})
        counts[result_val] = counts.get(result_val, 0) + 1

    return {"total": total, "blocked": blocked, "by_gate": by_gate}


def _legacy_result_from_report(report: GateReport, *, quality_score: QualityScore | None = None) -> QualityGatesResult:
    """Convert a detailed gate report back to the legacy wrapper result."""
    return QualityGatesResult(
        task_id=report.task_id,
        passed=report.overall_pass,
        gate_results=[
            QualityGateCheckResult(
                gate=result.name,
                passed=result.status in {"pass", "skipped", "bypassed"},
                blocked=result.blocked,
                detail=result.details,
                status=result.status,
            )
            for result in report.results
        ],
        quality_score=quality_score,
    )


def _env_skip_gates() -> list[str] | None:
    raw = os.getenv("BERNSTEIN_SKIP_GATES", "").strip()
    if not raw:
        return None
    return [gate.strip() for gate in raw.split(",") if gate.strip()]


def _env_bypass_reason() -> str | None:
    raw = os.getenv("BERNSTEIN_SKIP_GATE_REASON", "").strip()
    return raw or None
