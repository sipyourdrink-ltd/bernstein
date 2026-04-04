"""Property tests for patch extraction and application.

Tests invariants of ``bernstein.testing.patch_harness.extract_patch`` and
``apply_patch`` — the canonical implementations used by the SWE-Bench harness
and any adapter that produces unified diffs.

Property tests here are parametrised over a ``PatchGenerator`` instead of a
random-input library, so the test suite stays deterministic and fast while
still covering the full contract surface of the patch application protocol.

Self-check (regression proof)
------------------------------
``test_harness_catches_regression`` verifies that the harness detects a known
bug: a regex pattern that accidentally matched non-diff fenced blocks.  If
``extract_patch`` is ever regressed to that broken implementation the property
test fails, proving the harness is load-bearing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from bernstein.testing.patch_harness import (
    ExtractionScenario,
    PatchGenerator,
    apply_patch,
    extract_patch,
)

# ---------------------------------------------------------------------------
# extract_patch — invariant properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", PatchGenerator.all_scenarios(), ids=str)
def test_extract_patch_empty_contract(scenario: ExtractionScenario) -> None:
    """Property: empty-expectation scenarios always return ``""``."""
    if not scenario.expect_empty:
        pytest.skip("not an empty-expectation scenario")
    result = extract_patch(scenario.input_text)
    assert result == "", f"Expected empty string for scenario {scenario.label!r}, got {result!r}"


@pytest.mark.parametrize("scenario", PatchGenerator.all_scenarios(), ids=str)
def test_extract_patch_nonempty_contract(scenario: ExtractionScenario) -> None:
    """Property: non-empty-expectation scenarios return content matching ``expected_content``."""
    if scenario.expect_empty:
        pytest.skip("not a non-empty-expectation scenario")
    result = extract_patch(scenario.input_text)
    assert result != "", f"Expected non-empty result for scenario {scenario.label!r}"
    if scenario.expected_content:
        assert result == scenario.expected_content, (
            f"Scenario {scenario.label!r}: got {result!r}, expected {scenario.expected_content!r}"
        )


@pytest.mark.parametrize("scenario", PatchGenerator.all_scenarios(), ids=str)
def test_extract_patch_result_is_always_stripped(scenario: ExtractionScenario) -> None:
    """Property: result is always stripped — no leading or trailing whitespace."""
    result = extract_patch(scenario.input_text)
    assert result == result.strip(), f"Result for {scenario.label!r} is not stripped: {result!r}"


@pytest.mark.parametrize("scenario", PatchGenerator.all_scenarios(), ids=str)
def test_extract_patch_result_never_contains_fence(scenario: ExtractionScenario) -> None:
    """Property: result never starts or ends with the ``` fence delimiter."""
    result = extract_patch(scenario.input_text)
    assert not result.startswith("```"), f"Result for {scenario.label!r} starts with fence"
    assert not result.endswith("```"), f"Result for {scenario.label!r} ends with fence"


def test_extract_patch_idempotent() -> None:
    """Property: applying extract_patch to its own output returns the same value."""
    text = "Here is the patch:\n\n```diff\ndiff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y\n```"
    first = extract_patch(text)
    # Wrapping the first result in a diff block and extracting again yields the same content.
    wrapped = f"```diff\n{first}\n```"
    second = extract_patch(wrapped)
    assert first == second


def test_extract_patch_multiple_blocks_returns_first() -> None:
    """Property: only the first diff block is returned when multiple are present."""
    block_a = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b"
    block_b = "diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n@@ -1 +1 @@\n-z\n+y"
    text = f"```diff\n{block_a}\n```\n\nSome prose.\n\n```diff\n{block_b}\n```"
    result = extract_patch(text)
    assert block_a.strip() in result
    assert block_b.strip() not in result


# ---------------------------------------------------------------------------
# apply_patch — invariant properties (require a real git repo in tmp_path)
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, filename: str, content: str) -> None:
    """Create a minimal git repo with one committed file."""
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True, cwd=str(path))
    subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True, cwd=str(path))
    (path / filename).write_text(content)
    subprocess.run(["git", "add", "."], check=True, capture_output=True, cwd=str(path))
    subprocess.run(["git", "commit", "-m", "init"], check=True, capture_output=True, cwd=str(path))


def test_apply_patch_empty_patch_returns_false(tmp_path: Path) -> None:
    """Property: an empty patch always returns False without touching the repo."""
    _init_git_repo(tmp_path, "target.py", "x = 1\n")
    assert apply_patch("", tmp_path) is False
    assert apply_patch("   \n\t\n  ", tmp_path) is False


def test_apply_patch_valid_patch_returns_true(tmp_path: Path) -> None:
    """Property: a well-formed patch against an existing file returns True."""
    old_content = "x = 1\n"
    _init_git_repo(tmp_path, "target.py", old_content)
    patch = PatchGenerator.valid_git_patch("target.py", old="x = 1\n", new="x = 2\n")
    result = apply_patch(patch, tmp_path)
    assert result is True
    assert (tmp_path / "target.py").read_text() == "x = 2\n"


def test_apply_patch_invalid_patch_returns_false(tmp_path: Path) -> None:
    """Property: a syntactically invalid patch returns False."""
    _init_git_repo(tmp_path, "target.py", "x = 1\n")
    result = apply_patch("this is not a valid unified diff\n", tmp_path)
    assert result is False


def test_apply_patch_mismatched_context_returns_false(tmp_path: Path) -> None:
    """Property: a patch with wrong context lines returns False (file has drifted)."""
    _init_git_repo(tmp_path, "target.py", "y = 99\n")  # different content than patch expects
    patch = PatchGenerator.valid_git_patch("target.py", old="x = 1\n", new="x = 2\n")
    result = apply_patch(patch, tmp_path)
    assert result is False


def test_apply_patch_nonexistent_file_returns_false(tmp_path: Path) -> None:
    """Property: a patch targeting a non-existent file returns False."""
    _init_git_repo(tmp_path, "other.py", "z = 0\n")
    patch = PatchGenerator.valid_git_patch("target.py", old="x = 1\n", new="x = 2\n")
    result = apply_patch(patch, tmp_path)
    assert result is False


def test_apply_patch_idempotency_second_application_fails(tmp_path: Path) -> None:
    """Property: applying the same patch twice fails on the second attempt."""
    old_content = "x = 1\n"
    _init_git_repo(tmp_path, "target.py", old_content)
    patch = PatchGenerator.valid_git_patch("target.py", old="x = 1\n", new="x = 2\n")
    assert apply_patch(patch, tmp_path) is True
    # File is now "x = 2\n" — the old context no longer matches.
    assert apply_patch(patch, tmp_path) is False


# ---------------------------------------------------------------------------
# Self-check: the harness catches a known regression
# ---------------------------------------------------------------------------


def test_harness_catches_regression() -> None:
    """Self-check: a broken regex that matches non-diff blocks is detected.

    This proves the harness is load-bearing: if ``extract_patch`` is regressed
    to a pattern that matches any fenced block (not just ``diff``), these
    assertions fail, exposing the regression before it reaches production.
    """
    # A broken implementation might use r"```(\w+)?\s*\n(.*?)```" and
    # accidentally return content from a ```python block.
    _BROKEN_RE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)

    def broken_extract(text: str) -> str:
        m = _BROKEN_RE.search(text)
        return m.group(2).strip() if m else ""

    python_block = "```python\nprint('not a patch')\n```"

    # The broken implementation returns content from a non-diff block.
    assert broken_extract(python_block) == "print('not a patch')"

    # The correct implementation returns "" for non-diff blocks.
    assert extract_patch(python_block) == "", (
        "extract_patch must return '' for non-diff fenced blocks — "
        "a regression to a generic block matcher would break this."
    )


def test_extract_patch_fenced_diff_keyword_required() -> None:
    """Property: only blocks explicitly labelled ``diff`` are extracted.

    Blocks labelled ``patch``, ``text``, or unlabelled must all return ``""``.
    """
    diff_content = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y"
    for lang in ("patch", "text", ""):
        block = f"```{lang}\n{diff_content}\n```"
        result = extract_patch(block)
        assert result == "", (
            f"Block labelled {lang!r} should not be extracted, but got {result!r}"
        )
