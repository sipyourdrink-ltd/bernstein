"""CI self-healing: detect failing CI jobs and create fix tasks.

This module provides utilities to:
- Parse CI failure output and classify the root cause
- Create a Bernstein fix task (posted to the task server or written to backlog)
- Generate a pre-push hook script that runs fast checks locally

The self-healing loop is:
  1. CI fails on push
  2. GitHub Actions "on-failure" job calls ``create_ci_fix_issue`` via gh CLI
  3. Next ``bernstein run`` picks up the ci-fix task (priority=1)
  4. QA agent reads the failure, fixes the root cause, pushes a new commit
"""

from __future__ import annotations

import logging
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.quality.ci_log_parser import CILogParser
    from bernstein.core.quality.ci_monitor import FailureContext

logger = logging.getLogger(__name__)


class CIFailureKind(Enum):
    """Categorised CI failure reasons."""

    RUFF_LINT = "ruff_lint"
    RUFF_FORMAT = "ruff_format"
    PYTEST = "pytest"
    PYRIGHT = "pyright"
    MISSING_FILE = "missing_file"
    IMPORT_ERROR = "import_error"
    UNKNOWN = "unknown"


@dataclass
class CIFailure:
    """A single parsed CI failure.

    Attributes:
        kind: Categorised failure type.
        job: Name of the failing CI job (lint, test, typecheck…).
        summary: Human-readable one-line summary.
        details: Raw failure output excerpt (first 2 KB).
        fix_hint: Suggested fix command or instruction.
        affected_files: Source files mentioned in the failure output.
    """

    kind: CIFailureKind
    job: str
    summary: str
    details: str = ""
    fix_hint: str = ""
    affected_files: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_FILE_RE = re.compile(r"src/\S+\.py")


def parse_failures(log: str, job: str = "ci") -> list[CIFailure]:
    """Extract structured failures from raw CI log output.

    Args:
        log: Raw text output from a CI step (stdout + stderr combined).
        job: Name of the CI job that produced this log.

    Returns:
        List of parsed ``CIFailure`` objects (may be empty).
    """
    failures: list[CIFailure] = []
    snippet = log[:2048]
    files = _FILE_RE.findall(log)

    if "ruff check" in log.lower() or re.search(r"E\d{3}|W\d{3}|F\d{3}|RUF\d{3}", log):
        failures.append(
            CIFailure(
                kind=CIFailureKind.RUFF_LINT,
                job=job,
                summary="ruff lint errors found",
                details=snippet,
                fix_hint="uv run ruff check --fix src/ && uv run ruff check src/",
                affected_files=list(dict.fromkeys(files)),
            )
        )
    if "would reformat" in log.lower():
        failures.append(
            CIFailure(
                kind=CIFailureKind.RUFF_FORMAT,
                job=job,
                summary="ruff format: files need reformatting",
                details=snippet,
                fix_hint="uv run ruff format src/",
                affected_files=list(dict.fromkeys(files)),
            )
        )
    if "filenotfounderror" in log.lower() or "no such file" in log.lower():
        missing = re.findall(r"FileNotFoundError[^\n]*", log, re.IGNORECASE)
        failures.append(
            CIFailure(
                kind=CIFailureKind.MISSING_FILE,
                job=job,
                summary=missing[0] if missing else "missing file",
                details=snippet,
                fix_hint="Check .gitignore — required files may be excluded from the repo",
                affected_files=list(dict.fromkeys(files)),
            )
        )
    if "importerror" in log.lower() or "modulenotfounderror" in log.lower():
        errors = re.findall(r"(?:ImportError|ModuleNotFoundError)[^\n]*", log)
        failures.append(
            CIFailure(
                kind=CIFailureKind.IMPORT_ERROR,
                job=job,
                summary=errors[0] if errors else "import error",
                details=snippet,
                fix_hint="Add missing package to pyproject.toml dependencies",
                affected_files=list(dict.fromkeys(files)),
            )
        )
    if "failed" in log.lower() and ("pytest" in log.lower() or "test_" in log.lower()):
        failures.append(
            CIFailure(
                kind=CIFailureKind.PYTEST,
                job=job,
                summary="pytest failures",
                details=snippet,
                fix_hint="uv run python scripts/run_tests.py -x",
                affected_files=list(dict.fromkeys(files)),
            )
        )
    if "error" in log.lower() and "pyright" in log.lower():
        failures.append(
            CIFailure(
                kind=CIFailureKind.PYRIGHT,
                job=job,
                summary="pyright type errors",
                details=snippet,
                fix_hint="uv run pyright 2>&1 | head -40",
                affected_files=list(dict.fromkeys(files)),
            )
        )
    if not failures:
        failures.append(
            CIFailure(
                kind=CIFailureKind.UNKNOWN,
                job=job,
                summary="CI failure (cause unknown)",
                details=snippet,
                fix_hint="Review the CI log manually",
            )
        )
    return failures


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------


def build_task_payload(failures: list[CIFailure], run_url: str = "") -> dict[str, Any]:
    """Build a Bernstein task payload from a list of CI failures.

    Args:
        failures: Parsed CI failures.
        run_url: URL of the failing CI run (for context).

    Returns:
        Dict ready to POST to ``/tasks``.
    """
    kinds = ", ".join(sorted({f.kind.value for f in failures}))
    summaries = "\n".join(f"- [{f.job}] {f.summary}" for f in failures)
    hints = "\n".join(f"  {f.fix_hint}" for f in failures if f.fix_hint)
    run_link = f"\nCI run: {run_url}" if run_url else ""

    description = textwrap.dedent(f"""\
        CI is failing. Failures detected: {kinds}

        ## Failures
        {summaries}

        ## Suggested fixes
        {hints}
        {run_link}

        ## Instructions
        1. Run the suggested fix commands locally.
        2. Verify with: uv run ruff check src/ && uv run python scripts/run_tests.py -x
        3. Commit and push.
    """)

    return {
        "title": f"[ci-fix] CI failing: {kinds}",
        "description": description,
        "role": "qa",
        "priority": 1,
        "scope": "small",
        "task_type": "fix",
    }


def post_ci_fix_task(server_url: str, failures: list[CIFailure], run_url: str = "") -> bool:
    """POST a ci-fix task to the Bernstein task server.

    Args:
        server_url: Base URL of the Bernstein task server.
        failures: Parsed CI failures.
        run_url: URL of the failing CI run.

    Returns:
        True if the task was created, False on error.
    """
    import httpx

    payload = build_task_payload(failures, run_url)
    try:
        r = httpx.post(f"{server_url}/tasks", json=payload, timeout=5)
        r.raise_for_status()
        return True
    except Exception:
        return False


def write_ci_fix_task(backlog_dir: Path, failures: list[CIFailure], run_url: str = "") -> Path:
    """Write a ci-fix task as a JSON file in the backlog directory.

    This is the offline fallback when no Bernstein server is running (e.g. in CI).

    Args:
        backlog_dir: Path to ``.sdd/backlog/open/``.
        failures: Parsed CI failures.
        run_url: URL of the failing CI run.

    Returns:
        Path to the written task file.
    """
    import json
    import time

    backlog_dir.mkdir(parents=True, exist_ok=True)
    payload = build_task_payload(failures, run_url)
    task_id = f"ci-fix-{int(time.time())}"
    payload["id"] = task_id
    payload["status"] = "open"
    payload["created_at"] = time.time()

    path = backlog_dir / f"{task_id}.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


# ---------------------------------------------------------------------------
# Pre-push hook
# ---------------------------------------------------------------------------

_HOOK_SCRIPT = """\
#!/usr/bin/env bash
# bernstein pre-push hook — runs fast CI checks before push
# Install: cp this to .git/hooks/pre-push && chmod +x .git/hooks/pre-push

set -e

echo "[bernstein] Running pre-push checks..."

# 1. Lint
echo "[bernstein] ruff check..."
uv run ruff check src/ || { echo "[bernstein] FAIL: ruff lint errors. Run: uv run ruff check --fix src/"; exit 1; }

# 2. Format
echo "[bernstein] ruff format..."
uv run ruff format --check src/ || { echo "[bernstein] FAIL: format issues. Run: uv run ruff format src/"; exit 1; }

# 3. Tests (unit only — fast)
echo "[bernstein] pytest unit tests..."
uv run python scripts/run_tests.py -x || {
    echo "[bernstein] FAIL: tests failed. Run: uv run python scripts/run_tests.py -x"
    exit 1
}

echo "[bernstein] All pre-push checks passed."
"""


def install_pre_push_hook(repo_root: Path, force: bool = False) -> bool:
    """Install the Bernstein pre-push hook into the repo's git hooks directory.

    Args:
        repo_root: Root of the git repository.
        force: Overwrite an existing hook if True.

    Returns:
        True if installed, False if already exists and force=False.
    """
    hook_path = repo_root / ".git" / "hooks" / "pre-push"
    if hook_path.exists() and not force:
        return False
    hook_path.write_text(_HOOK_SCRIPT)
    hook_path.chmod(0o755)
    return True


# ---------------------------------------------------------------------------
# Doctor checks
# ---------------------------------------------------------------------------


def check_test_dependencies() -> list[dict[str, str]]:
    """Check that all CI tool dependencies are importable/executable.

    Returns:
        List of check result dicts with keys: name, ok (bool), detail, fix.
    """
    checks: list[dict[str, str]] = []

    # ruff
    result = subprocess.run(
        ["uv", "run", "ruff", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    ruff_ok = result.returncode == 0
    checks.append(
        {
            "name": "ruff",
            "ok": str(ruff_ok),
            "detail": result.stdout.strip() if ruff_ok else result.stderr.strip()[:80],
            "fix": "" if ruff_ok else "Add ruff to [dependency-groups] dev in pyproject.toml",
        }
    )

    # pytest
    result = subprocess.run(
        ["uv", "run", "pytest", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    pytest_ok = result.returncode == 0
    checks.append(
        {
            "name": "pytest",
            "ok": str(pytest_ok),
            "detail": result.stdout.strip()[:60] if pytest_ok else result.stderr.strip()[:80],
            "fix": "" if pytest_ok else "Add pytest to [dependency-groups] dev in pyproject.toml",
        }
    )

    # pyright
    result = subprocess.run(
        ["uv", "run", "pyright", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    pyright_ok = result.returncode == 0
    checks.append(
        {
            "name": "pyright",
            "ok": str(pyright_ok),
            "detail": result.stdout.strip()[:60] if pyright_ok else result.stderr.strip()[:80],
            "fix": "" if pyright_ok else "Add pyright to [dependency-groups] dev in pyproject.toml",
        }
    )

    return checks


# ---------------------------------------------------------------------------
# Log download
# ---------------------------------------------------------------------------


def download_github_actions_log(
    run_url: str,
    *,
    timeout: int = 60,
) -> str:
    """Download the failed-step log from a GitHub Actions run.

    Delegates to the GitHub Actions adapter.  This is a convenience
    wrapper so callers do not need to import the adapter directly.

    Args:
        run_url: URL of the GitHub Actions run, e.g.
            ``https://github.com/owner/repo/actions/runs/123456``.
        timeout: Subprocess timeout in seconds.

    Returns:
        Raw log text from the failed steps.

    Raises:
        RuntimeError: If the ``gh`` command fails.
    """
    from bernstein.adapters.ci.github_actions import (
        download_github_actions_log as _download,
    )

    return _download(run_url, timeout=timeout)


# ---------------------------------------------------------------------------
# CI Fix Pipeline
# ---------------------------------------------------------------------------


class CIFixResult(Enum):
    """Outcome of a single pipeline iteration."""

    TASK_CREATED = "task_created"
    NO_FAILURES = "no_failures"
    MAX_RETRIES = "max_retries"
    DOWNLOAD_ERROR = "download_error"


@dataclass
class CIFixAttempt:
    """Record of a single fix attempt.

    Attributes:
        attempt: 1-based attempt number.
        failures: Failures parsed from the CI log.
        result: Outcome of this attempt.
        timestamp: Unix timestamp of the attempt.
        task_id: ID of the created fix task (if any).
        error: Error message if something went wrong.
    """

    attempt: int
    failures: list[CIFailure]
    result: CIFixResult
    timestamp: float = field(default_factory=time.time)
    task_id: str = ""
    error: str = ""


@dataclass
class CIFixPipeline:
    """Orchestrates the full CI-failure-to-fix-task loop.

    Usage::

        pipeline = CIFixPipeline(
            server_url="http://127.0.0.1:8052",
            max_retries=3,
        )
        # From a CI run URL:
        attempts = pipeline.run_from_url("https://github.com/.../runs/123")

        # Or from a raw log:
        attempts = pipeline.run_from_log(raw_log_text)

    Attributes:
        server_url: Base URL of the Bernstein task server.
        max_retries: Maximum number of fix attempts before giving up.
        parser: Optional CI log parser override.  When ``None``, the
            default ``GitHubActionsParser`` is used for URL-based runs
            and the core ``parse_failures`` is used for raw logs.
        backlog_dir: When set, write tasks as files instead of POSTing
            to the server (offline mode).
    """

    server_url: str = "http://127.0.0.1:8052"
    max_retries: int = 3
    parser: CILogParser | None = None
    backlog_dir: Path | None = None

    def run_from_url(self, run_url: str) -> list[CIFixAttempt]:
        """Download a CI log and create fix task(s).

        This is a single-shot method: it downloads the log once, parses
        it, and creates at most one fix task.  To retry after the agent
        has pushed a fix, call this method again — the pipeline tracks
        attempts internally via the returned list.

        Args:
            run_url: GitHub Actions run URL.

        Returns:
            List with one ``CIFixAttempt`` (for caller bookkeeping).
        """
        attempt_num = 1
        try:
            raw_log = download_github_actions_log(run_url)
        except Exception as exc:
            return [
                CIFixAttempt(
                    attempt=attempt_num,
                    failures=[],
                    result=CIFixResult.DOWNLOAD_ERROR,
                    error=str(exc),
                )
            ]

        return self._process_log(raw_log, run_url=run_url, attempt=attempt_num)

    def run_from_log(self, raw_log: str, *, run_url: str = "") -> list[CIFixAttempt]:
        """Parse an already-downloaded CI log and create fix task(s).

        Args:
            raw_log: Raw CI log text.
            run_url: Optional URL for context.

        Returns:
            List with one ``CIFixAttempt``.
        """
        return self._process_log(raw_log, run_url=run_url, attempt=1)

    def run_loop(
        self,
        raw_log: str,
        *,
        run_url: str = "",
    ) -> list[CIFixAttempt]:
        """Run the fix loop up to ``max_retries`` times on the same log.

        Each iteration creates a fix task.  The loop stops when either
        no failures are found or the retry limit is reached.

        Note: in real usage the agent would push a fix between iterations,
        and you would call ``run_from_url`` with the *new* CI run.  This
        method is primarily useful for testing the retry-limit logic.

        Args:
            raw_log: Raw CI log text.
            run_url: Optional URL for context.

        Returns:
            List of all ``CIFixAttempt`` records.
        """
        attempts: list[CIFixAttempt] = []
        for i in range(1, self.max_retries + 1):
            result = self._process_log(raw_log, run_url=run_url, attempt=i)
            attempts.extend(result)
            last = result[-1] if result else None
            if last and last.result == CIFixResult.NO_FAILURES:
                break
        else:
            # Exhausted retries — mark the last attempt.
            if attempts and attempts[-1].result == CIFixResult.TASK_CREATED:
                attempts.append(
                    CIFixAttempt(
                        attempt=self.max_retries + 1,
                        failures=attempts[-1].failures,
                        result=CIFixResult.MAX_RETRIES,
                    )
                )
        return attempts

    # -- internal helpers --------------------------------------------------

    def _process_log(
        self,
        raw_log: str,
        *,
        run_url: str,
        attempt: int,
    ) -> list[CIFixAttempt]:
        """Core logic: parse log, create task, return attempt record.

        Args:
            raw_log: Raw CI log text.
            run_url: CI run URL (for context in the task).
            attempt: 1-based attempt number.

        Returns:
            Single-element list with the attempt record.
        """
        failures = self._parse(raw_log)
        if not failures:
            return [
                CIFixAttempt(
                    attempt=attempt,
                    failures=[],
                    result=CIFixResult.NO_FAILURES,
                )
            ]

        task_id = self._create_task(failures, run_url=run_url)
        return [
            CIFixAttempt(
                attempt=attempt,
                failures=failures,
                result=CIFixResult.TASK_CREATED,
                task_id=task_id,
            )
        ]

    def _parse(self, raw_log: str) -> list[CIFailure]:
        """Parse failures using the configured parser or the default.

        Args:
            raw_log: Raw CI log text.

        Returns:
            List of ``CIFailure`` objects.  An ``UNKNOWN`` failure with
            no actionable content is filtered out.
        """
        failures = self.parser.parse(raw_log) if self.parser is not None else parse_failures(raw_log)

        # Filter out a lone UNKNOWN with empty details (means "no real failure").
        if len(failures) == 1 and failures[0].kind == CIFailureKind.UNKNOWN and not failures[0].details.strip():
            return []
        return failures

    def _create_task(
        self,
        failures: list[CIFailure],
        *,
        run_url: str,
    ) -> str:
        """Create a fix task via the server or the backlog directory.

        Args:
            failures: Parsed CI failures.
            run_url: CI run URL.

        Returns:
            Task ID string (from server response or generated filename).
        """
        payload = build_task_payload(failures, run_url)
        payload["role"] = "ci-fixer"

        if self.backlog_dir is not None:
            path = write_ci_fix_task(self.backlog_dir, failures, run_url)
            task_id = path.stem
            logger.info("CI fix task written to backlog: %s", task_id)
            return task_id

        ok = post_ci_fix_task(self.server_url, failures, run_url)
        if ok:
            task_id = f"ci-fix-{int(time.time())}"
            logger.info("CI fix task posted to server: %s", task_id)
            return task_id

        logger.warning("Failed to post CI fix task to server; writing to fallback")
        return ""


# ---------------------------------------------------------------------------
# CI Autofix Pipeline — integrates CIMonitor with task creation and PR flow
# ---------------------------------------------------------------------------


@dataclass
class CIAutofixPipeline:
    """End-to-end pipeline: ``FailureContext`` → Bernstein task → fix PR.

    Takes a ``FailureContext`` from ``CIMonitor``, creates a Bernstein
    task with the failure context embedded in the description, and
    provides ``create_fix_pr`` to open a GitHub PR after the fix is
    applied.

    Usage::

        from bernstein.core.quality.ci_monitor import FailureContext
        from bernstein.core.quality.ci_fix import CIAutofixPipeline

        pipeline = CIAutofixPipeline(server_url="http://127.0.0.1:8052")
        task_id = pipeline.create_fix_task(failure_ctx)
        pr_url = pipeline.create_fix_pr(task_id, failure_ctx, cwd=repo_root)

    Attributes:
        server_url: Base URL of the Bernstein task server.
        repo_root: Path to the repository root (for git operations).
    """

    server_url: str = "http://127.0.0.1:8052"
    repo_root: Path | None = None

    def create_fix_task(
        self,
        failure: FailureContext,
        *,
        run_url: str = "",
    ) -> str:
        """Create a Bernstein task from a ``FailureContext``.

        Args:
            failure: Parsed failure context from CI logs.
            run_url: Optional URL of the CI run for traceability.

        Returns:
            Task ID of the created task (empty string on failure).
        """
        import httpx

        description = self._build_description(failure, run_url)
        payload = {
            "title": f"[ci-autofix] Fix: {failure.error_message[:80]}",
            "description": description,
            "role": "qa",
            "priority": 1,
            "scope": "small",
            "task_type": "fix",
        }

        try:
            r = httpx.post(
                f"{self.server_url}/tasks",
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            task_id: str = data.get("id", f"ci-autofix-{int(time.time())}")
            logger.info("CI autofix task created: %s", task_id)
            return task_id
        except Exception:
            logger.exception("Failed to create CI autofix task")
            return ""

    def create_fix_pr(
        self,
        task_id: str,
        failure: FailureContext,
        *,
        cwd: Path | None = None,
        branch: str = "",
        base: str = "main",
    ) -> str:
        """Create a GitHub PR for a completed CI fix task.

        Delegates to ``git_pr.create_github_pr`` for the actual PR
        creation via the ``gh`` CLI.

        Args:
            task_id: ID of the completed fix task.
            failure: Original failure context (used for PR title/body).
            cwd: Repository root override (falls back to ``self.repo_root``).
            branch: Source branch name.  If empty, derived from task ID.
            base: Target branch (default ``"main"``).

        Returns:
            PR URL on success, empty string on failure.
        """
        from bernstein.core.git_pr import create_github_pr

        repo = cwd or self.repo_root
        if repo is None:
            logger.error("No repository root provided for PR creation")
            return ""

        head = branch or f"bernstein/ci-fix-{task_id}"
        title = f"fix(ci): {failure.error_message[:72]}"
        body = self._build_pr_body(failure, task_id)

        result = create_github_pr(
            repo,
            title=title,
            body=body,
            head=head,
            base=base,
            labels=["ci-fix", "auto-generated"],
        )
        if result.success:
            logger.info("CI fix PR created: %s", result.pr_url)
            return result.pr_url

        logger.warning("Failed to create CI fix PR: %s", result.error)
        return ""

    @staticmethod
    def _build_description(failure: FailureContext, run_url: str) -> str:
        """Build a task description from failure context.

        Args:
            failure: Parsed failure context.
            run_url: CI run URL.

        Returns:
            Formatted task description string.
        """
        parts = [
            "CI is failing. Auto-detected failure details:\n",
            f"**Test:** {failure.test_name}" if failure.test_name else "",
            f"**Error:** {failure.error_message}" if failure.error_message else "",
            f"**File:** {failure.file_path}:{failure.line_number}" if failure.file_path else "",
        ]
        if failure.stack_trace:
            parts.append(f"\n```\n{failure.stack_trace[:2000]}\n```")
        if run_url:
            parts.append(f"\nCI run: {run_url}")
        parts.append(
            "\n## Instructions\n"
            "1. Read the error and traceback above.\n"
            "2. Fix the root cause in the source file.\n"
            "3. Run tests locally to verify.\n"
            "4. Commit and push."
        )
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _build_pr_body(failure: FailureContext, task_id: str) -> str:
        """Build a PR body from failure context.

        Args:
            failure: Parsed failure context.
            task_id: Bernstein task ID.

        Returns:
            Formatted PR body string.
        """
        parts = [
            f"Fixes CI failure detected by Bernstein (task `{task_id}`).\n",
            f"**Failed test:** {failure.test_name}" if failure.test_name else "",
            f"**Error:** {failure.error_message}" if failure.error_message else "",
            f"**Location:** `{failure.file_path}:{failure.line_number}`" if failure.file_path else "",
        ]
        return "\n".join(p for p in parts if p)
