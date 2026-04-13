"""Automated integration test generation for agent-produced code changes.

After an agent completes a task, this module generates integration tests that
verify the changed code paths work in context (not just in isolation). It uses
an LLM to write a pytest test file, runs it, and fails the quality gate if the
generated test does not pass.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "google/gemini-flash-1.5"
_DEFAULT_PROVIDER = "openrouter"
_MAX_DIFF_CHARS = 12_000
_MAX_TOKENS = 2_048
_TEST_TIMEOUT_S = 120

_PROMPT_TEMPLATE = """\
You are an expert Python test engineer. An AI coding agent just made the \
following changes to a Python codebase. Your job is to write a pytest \
**integration test** that exercises the changed code path in context — \
using real imports, real function calls, and real data where feasible. \
Do NOT write unit tests with mocks; write integration tests that actually \
invoke the changed code.

## Task Description
**Title:** {title}
**Description:**
{description}

## Git Diff (changed code)
```diff
{diff}
```

## Instructions
1. Write ONE pytest test function named `test_integration_<slug>` where \
`<slug>` is a short snake_case identifier for this change.
2. Import the modules that were changed directly. Use `sys.path` manipulation \
if needed but prefer standard imports.
3. The test must call the changed functions/classes and assert meaningful \
postconditions — not trivial `assert True`.
4. Keep the test self-contained: create any required fixtures inline.
5. If the diff touches a web server, CLI, or external service, test via the \
public Python API (not the network) by calling the underlying function.
6. Output ONLY valid Python code. No markdown fences, no explanations.
7. Start the file with: `# auto-generated integration test — do not edit`

Output ONLY the Python test file content.
"""


@dataclass(frozen=True)
class IntegTestGenConfig:
    """Configuration for the integration test generation quality gate.

    Attributes:
        enabled: Master switch.
        model: LLM model for test generation.
        provider: LLM provider key passed to call_llm.
        max_diff_chars: Truncate diff at this length for cost control.
        max_tokens: Token cap for the LLM response.
        test_timeout_s: Timeout in seconds for the generated test run.
        block_on_fail: Whether a failing generated test blocks the task.
        write_tests: If True, persist the generated test to tests/integration/.
    """

    enabled: bool = False
    model: str = _DEFAULT_MODEL
    provider: str = _DEFAULT_PROVIDER
    max_diff_chars: int = _MAX_DIFF_CHARS
    max_tokens: int = _MAX_TOKENS
    test_timeout_s: int = _TEST_TIMEOUT_S
    block_on_fail: bool = True
    write_tests: bool = False


@dataclass
class IntegTestGenResult:
    """Result of the integration test generation gate.

    Attributes:
        passed: Whether the generated test passed.
        blocked: Whether the gate blocks the task.
        detail: Human-readable summary.
        test_code: The generated test source code.
        test_path: Path to the written test file (if write_tests=True).
        pytest_output: Raw pytest output.
        errors: Any errors encountered during generation/execution.
    """

    passed: bool
    blocked: bool
    detail: str
    test_code: str = ""
    test_path: str = ""
    pytest_output: str = ""
    errors: list[str] = field(default_factory=list)


def _extract_python_code(raw: str) -> str:
    """Strip markdown fences from LLM output and return bare Python code."""
    # Remove ```python ... ``` or ``` ... ``` fences
    fenced = re.sub(r"^```(?:python)?\s*\n", "", raw.strip(), flags=re.MULTILINE)
    fenced = re.sub(r"\n```\s*$", "", fenced.strip(), flags=re.MULTILINE)
    return fenced.strip()


def _get_diff(run_dir: Path, base_ref: str = "HEAD~1") -> str:
    """Return a git diff of changed Python files."""
    try:
        result = subprocess.run(
            ["git", "diff", base_ref, "--", "*.py"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=run_dir,
            timeout=30,
        )
        if result.returncode != 0:
            # Fall back to staged diff
            result = subprocess.run(
                ["git", "diff", "--cached", "--", "*.py"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=run_dir,
                timeout=30,
            )
        return result.stdout or ""
    except Exception as exc:
        logger.warning("Failed to get diff for integration test gen: %s", exc)
        return ""


def _slug_from_title(title: str) -> str:
    """Convert a task title to a snake_case slug for test naming."""
    slug = re.sub(r"[^a-zA-Z0-9\s]", "", title.lower())
    slug = re.sub(r"\s+", "_", slug.strip())
    return slug[:40] or "change"


async def generate_and_run(
    task: Task,
    run_dir: Path,
    config: IntegTestGenConfig,
) -> IntegTestGenResult:
    """Generate an integration test, run it, and return the gate result.

    Args:
        task: The completed task whose changes should be tested.
        run_dir: Repository root.
        config: Gate configuration.

    Returns:
        IntegTestGenResult with pass/fail status and details.
    """
    from bernstein.core.llm import call_llm

    errors: list[str] = []

    # 1. Get the diff
    diff = _get_diff(run_dir)
    if not diff.strip():
        return IntegTestGenResult(
            passed=True,
            blocked=False,
            detail="No Python changes detected — skipping integration test generation.",
        )

    diff_truncated = diff[: config.max_diff_chars]

    # 2. Build the prompt
    description = getattr(task, "description", "") or ""
    title = getattr(task, "title", task.id)
    prompt = _PROMPT_TEMPLATE.format(
        title=title,
        description=description[:2000],
        diff=diff_truncated,
    )

    # 3. Generate test code via LLM
    try:
        raw = await call_llm(
            prompt,
            model=config.model,
            provider=config.provider,
            max_tokens=config.max_tokens,
            temperature=0.2,
        )
    except Exception as exc:
        errors.append(f"LLM call failed: {exc}")
        return IntegTestGenResult(
            passed=False,
            blocked=config.block_on_fail,
            detail=f"Integration test generation failed (LLM error): {exc}",
            errors=errors,
        )

    test_code = _extract_python_code(raw)
    if not test_code or "def test_" not in test_code:
        errors.append("LLM did not produce a valid test function.")
        return IntegTestGenResult(
            passed=False,
            blocked=config.block_on_fail,
            detail="Integration test generation failed: no valid test function in LLM output.",
            test_code=test_code,
            errors=errors,
        )

    # 4. Write test to temp file (or persistent location)
    slug = _slug_from_title(title)
    test_path_str = ""

    if config.write_tests:
        persist_dir = run_dir / "tests" / "integration" / "generated"
        persist_dir.mkdir(parents=True, exist_ok=True)
        test_file = persist_dir / f"test_gen_{slug}_{task.id[:8]}.py"
        test_file.write_text(test_code, encoding="utf-8")
        test_path_str = str(test_file)
        logger.info("Persisted generated integration test: %s", test_file)

    # 5. Run the test in a temp file (wrapped for async context)
    def _write_temp_file() -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix=f"test_integ_{slug}_",
            dir=run_dir / "tests" / "integration",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(test_code)
            return Path(tmp.name)

    tmp_path = await asyncio.to_thread(_write_temp_file)

    try:
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "pytest",
            str(tmp_path),
            "-x",
            "-q",
            "--tb=short",
            "--no-header",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=run_dir,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=config.test_timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return IntegTestGenResult(
                passed=False,
                blocked=config.block_on_fail,
                detail=f"Generated integration test timed out after {config.test_timeout_s}s.",
                test_code=test_code,
                test_path=test_path_str,
                errors=["Test execution timed out."],
            )

        pytest_output = (stdout_bytes or b"").decode(errors="replace")
        pytest_stderr = (stderr_bytes or b"").decode(errors="replace")
        full_output = pytest_output + (f"\n{pytest_stderr}" if pytest_stderr.strip() else "")

        passed = proc.returncode == 0
        if passed:
            detail = f"Generated integration test passed. Slug: test_integration_{slug}"
        else:
            detail = f"Generated integration test FAILED (exit {proc.returncode}). Output:\n{full_output[:1000]}"

        return IntegTestGenResult(
            passed=passed,
            blocked=config.block_on_fail and not passed,
            detail=detail,
            test_code=test_code,
            test_path=test_path_str,
            pytest_output=full_output,
            errors=errors,
        )

    finally:
        # Clean up temp file unless write_tests is on (already written elsewhere)
        if not config.write_tests and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def run_integration_test_gen_sync(
    task: Task,
    run_dir: Path,
    config: IntegTestGenConfig,
) -> IntegTestGenResult:
    """Synchronous wrapper for generate_and_run (for use in gate_runner)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, generate_and_run(task, run_dir, config))
                return future.result(timeout=config.test_timeout_s + 30)
        else:
            return loop.run_until_complete(generate_and_run(task, run_dir, config))
    except Exception as exc:
        logger.exception("Integration test generation gate crashed: %s", exc)
        return IntegTestGenResult(
            passed=False,
            blocked=config.block_on_fail,
            detail=f"Integration test generation gate error: {exc}",
            errors=[str(exc)],
        )
