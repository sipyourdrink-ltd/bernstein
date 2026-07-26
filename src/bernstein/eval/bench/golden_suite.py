"""
bernstein-bench: starter suite from existing ``golden.py`` curation.

These tasks are a representative sample of the real golden task set.
They are intentionally hermetic: no network calls, no real adapters —
the mock adapter in ``runner.py`` can execute them.

In production, ``bernstein bench run golden-v1`` loads this suite,
runs each task via the real ``scenario_runner.py`` adapter, and emits
a signed :class:`SubmissionBundle`.
"""

from __future__ import annotations

from bernstein.eval.bench.suite import BenchSuite, BenchTask


def build_golden_suite_v1() -> BenchSuite:
    """Return the canonical ``golden-v1`` benchmark suite."""
    tasks = [
        BenchTask(
            id="file_io_read_write",
            description=("Create a file, write deterministic content to it, read it back, and assert byte-equality."),
            steps=(
                "write 'hello bernstein\\n' to /tmp/bench_test.txt",
                "read /tmp/bench_test.txt",
                "assert content == 'hello bernstein\\n'",
            ),
            assertions=(
                {"kind": "file_exists", "path": "/tmp/bench_test.txt"},
                {"kind": "content_eq", "path": "/tmp/bench_test.txt", "expected": "hello bernstein\n"},
            ),
            category="file_io",
        ),
        BenchTask(
            id="refactor_rename_symbol",
            description=("In a Python snippet, rename function `foo` to `bar` and confirm all call-sites are updated."),
            steps=(
                "parse the Python source and identify all references to `foo`",
                "rename `foo` → `bar` in definitions and call-sites",
                "run a syntax check on the result",
            ),
            assertions=(
                {"kind": "no_symbol", "symbol": "foo"},
                {"kind": "symbol_present", "symbol": "bar"},
                {"kind": "syntax_valid"},
            ),
            category="refactor",
        ),
        BenchTask(
            id="test_generation_happy_path",
            description=(
                "Given a pure function `add(a, b) -> int`, generate a pytest test that exercises the happy path."
            ),
            steps=(
                "identify the function signature",
                "generate a pytest test module with at least one test",
                "assert the generated test imports the function and calls it",
            ),
            assertions=(
                {"kind": "file_exists", "path": "test_add.py"},
                {"kind": "contains", "path": "test_add.py", "text": "def test_"},
                {"kind": "contains", "path": "test_add.py", "text": "add("},
            ),
            category="test_generation",
        ),
        BenchTask(
            id="lint_fix_unused_import",
            description=("Remove an unused import from a Python file without altering any other lines."),
            steps=(
                "detect unused imports via static analysis",
                "remove the unused import line",
                "confirm no other lines were changed",
            ),
            assertions=(
                {"kind": "no_flake8_F401"},
                {"kind": "line_count_delta", "max_delta": -1},
            ),
            category="lint",
        ),
        BenchTask(
            id="doc_update_docstring",
            description=(
                "Add a one-line docstring to a function that currently has none, matching a provided spec string."
            ),
            steps=(
                "locate the function with no docstring",
                "insert the docstring as the first statement in the body",
                "verify with ast.get_docstring",
            ),
            assertions=(
                {"kind": "has_docstring", "function": "target_fn"},
                {"kind": "docstring_contains", "function": "target_fn", "text": "Compute"},
            ),
            category="documentation",
        ),
    ]
    return BenchSuite(version="golden-v1", tasks=tasks)
