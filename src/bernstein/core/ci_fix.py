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

import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


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
                fix_hint="uv run pytest tests/ -x -q --tb=short",
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
        CI is failing on master. Failures detected: {kinds}

        ## Failures
        {summaries}

        ## Suggested fixes
        {hints}
        {run_link}

        ## Instructions
        1. Run the suggested fix commands locally.
        2. Verify with: uv run ruff check src/ && uv run pytest tests/ -x -q
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
uv run pytest tests/unit/ -x -q --tb=short || {
    echo "[bernstein] FAIL: tests failed. Run: uv run pytest tests/unit/ -x -q --tb=short"
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
