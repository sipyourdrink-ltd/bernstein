"""Patch application harness for protocol and extension testing.

Provides ``extract_patch`` and ``apply_patch`` — the canonical implementations
used by the SWE-Bench harness and any adapter that produces unified diffs —
together with a ``PatchGenerator`` that drives parametrised property tests.

The harness is the seam: callers that validate patch protocols import from here
rather than from the private SWE-Bench internals, so the contract is tested in
one place and refactors don't silently break consumers.

Usage in tests::

    from bernstein.testing.patch_harness import PatchGenerator, apply_patch, extract_patch

    @pytest.mark.parametrize("scenario", PatchGenerator.all_scenarios())
    def test_extract_invariant(scenario):
        result = extract_patch(scenario.input_text)
        assert scenario.expect_empty == (result == "")
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Core patch operations (canonical implementations)
# ---------------------------------------------------------------------------

_DIFF_BLOCK_RE: Final = re.compile(r"```diff\s*\n(.*?)```", re.DOTALL)


def extract_patch(text: str) -> str:
    """Return the content of the first ```diff … ``` fenced block in *text*.

    Returns an empty string when no such block exists.  The result is always
    stripped of leading and trailing whitespace.

    This is the canonical implementation — the SWE-Bench harness delegates
    to this function so the contract lives in exactly one place.
    """
    m = _DIFF_BLOCK_RE.search(text)
    return m.group(1).strip() if m else ""


def apply_patch(patch: str, workdir: Path) -> bool:
    """Apply a unified diff to *workdir* using ``git apply``.

    Returns ``True`` when the patch is non-empty and applies cleanly.
    Returns ``False`` for empty/whitespace-only patches or patches that
    fail ``git apply --check``.

    Raises ``subprocess.CalledProcessError`` only if the final ``git apply``
    call fails after ``--check`` has already passed (should not happen in
    practice).
    """
    if not patch.strip():
        return False

    check = subprocess.run(
        ["git", "apply", "--check"],
        input=patch,
        text=True,
        capture_output=True,
        cwd=str(workdir),
    )
    if check.returncode != 0:
        return False

    subprocess.run(
        ["git", "apply"],
        input=patch,
        text=True,
        check=True,
        cwd=str(workdir),
    )
    return True


# ---------------------------------------------------------------------------
# Scenario dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionScenario:
    """One parametrised input for ``extract_patch`` property tests.

    Args:
        label: Human-readable name shown in pytest output.
        input_text: Text passed to ``extract_patch``.
        expect_empty: ``True`` when the result must be ``""``.
        expected_content: Exact expected content (ignored when *expect_empty*).
    """

    label: str
    input_text: str
    expect_empty: bool
    expected_content: str = ""

    def __str__(self) -> str:
        return self.label


# ---------------------------------------------------------------------------
# PatchGenerator — drives parametrised property tests
# ---------------------------------------------------------------------------


class PatchGenerator:
    """Generator of ``ExtractionScenario`` objects for property testing.

    Call ``PatchGenerator.all_scenarios()`` in a ``@pytest.mark.parametrize``
    decorator to cover the full contract surface of ``extract_patch``.
    """

    # Minimal valid unified diff that only renames a comment
    _MINIMAL_DIFF: Final[str] = textwrap.dedent(
        """\
        diff --git a/foo.py b/foo.py
        index 0000000..1111111 100644
        --- a/foo.py
        +++ b/foo.py
        @@ -1,1 +1,1 @@
        -# old comment
        +# new comment
        """
    )

    @classmethod
    def _fenced(cls, content: str) -> str:
        """Wrap *content* in a ```diff … ``` block."""
        return f"```diff\n{content}\n```"

    @classmethod
    def all_scenarios(cls) -> list[ExtractionScenario]:
        """Return all parametrised extraction scenarios.

        Covers:
        * empty / whitespace-only inputs → always returns ``""``
        * text with no fenced block → always returns ``""``
        * single diff block → returns block content (stripped)
        * multiple diff blocks → returns only the *first*
        * surrounding prose → irrelevant to extraction
        * diff block with extra blank lines → result is stripped
        """
        diff = cls._MINIMAL_DIFF.strip()

        return [
            # --- empty / no-block cases ---
            ExtractionScenario(
                label="empty_string",
                input_text="",
                expect_empty=True,
            ),
            ExtractionScenario(
                label="whitespace_only",
                input_text="   \n\t\n  ",
                expect_empty=True,
            ),
            ExtractionScenario(
                label="plain_prose_no_code_block",
                input_text="I made some changes to fix the bug.\nNo diff here.",
                expect_empty=True,
            ),
            ExtractionScenario(
                label="python_code_block_not_diff",
                input_text="```python\nprint('hello')\n```",
                expect_empty=True,
            ),
            ExtractionScenario(
                label="bash_code_block_not_diff",
                input_text="```bash\ngit status\n```",
                expect_empty=True,
            ),
            ExtractionScenario(
                label="unclosed_diff_block",
                input_text="```diff\n" + diff,  # missing closing ```
                expect_empty=True,
            ),
            # --- single block ---
            ExtractionScenario(
                label="single_diff_block_plain",
                input_text=cls._fenced(diff),
                expect_empty=False,
                expected_content=diff,
            ),
            ExtractionScenario(
                label="single_diff_block_with_prose_before",
                input_text="Here is the fix:\n\n" + cls._fenced(diff),
                expect_empty=False,
                expected_content=diff,
            ),
            ExtractionScenario(
                label="single_diff_block_with_prose_after",
                input_text=cls._fenced(diff) + "\n\nLet me know if this looks right.",
                expect_empty=False,
                expected_content=diff,
            ),
            ExtractionScenario(
                label="diff_block_with_leading_blank_lines",
                input_text="```diff\n\n\n" + diff + "\n```",
                expect_empty=False,
                expected_content=diff,  # strip removes surrounding blanks
            ),
            # --- multiple blocks → only first is returned ---
            ExtractionScenario(
                label="two_diff_blocks_returns_first",
                input_text=cls._fenced(diff) + "\n\nAlternatively:\n\n" + cls._fenced("diff --git a/bar.py b/bar.py\n"),
                expect_empty=False,
                expected_content=diff,
            ),
        ]

    @classmethod
    def valid_git_patch(cls, filename: str = "target.py", old: str = "x = 1\n", new: str = "x = 2\n") -> str:
        """Return a minimal unified diff that changes *old* to *new* in *filename*.

        The diff is suitable for use with ``apply_patch`` against a real git
        repository that already contains *filename* with *old* as its content.
        """
        old_lines: list[str] = old.splitlines(keepends=True)
        new_lines: list[str] = new.splitlines(keepends=True)
        old_count = len(old_lines)
        new_count = len(new_lines)
        hunk_lines: list[str] = [f"-{line}" for line in old_lines] + [f"+{line}" for line in new_lines]
        hunk = "".join(hunk_lines)
        return (
            f"diff --git a/{filename} b/{filename}\n"
            f"index 0000000..1111111 100644\n"
            f"--- a/{filename}\n"
            f"+++ b/{filename}\n"
            f"@@ -1,{old_count} +1,{new_count} @@\n"
            f"{hunk}"
        )
