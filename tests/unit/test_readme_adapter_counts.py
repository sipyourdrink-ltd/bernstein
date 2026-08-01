"""Guard the adapter counts the README states against the code they describe.

The README's "supported agents" paragraph makes two countable claims:

1. ``docs/adapters/index.md`` carries install commands for N adapters.
2. ``bernstein integrations list`` enumerates M wired-in adapters.

Both numbers drift silently when an adapter is added or the install matrix
gains a row. These tests recompute each number from its source - the markdown
table for N, ``integrations_cmd._enumerate_rows()`` for M - so a stale README
count fails here instead of shipping as a claim the code cannot back.

When the count legitimately changes, update the README sentence and these
tests will pass again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bernstein.cli.commands.integrations_cmd import _enumerate_rows

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
ADAPTER_INDEX = REPO_ROOT / "docs" / "adapters" / "index.md"

# "carries install commands for 29 of them"
_MATRIX_CLAIM_RE = re.compile(r"carries install commands for (\d+) of them")
# "enumerates all 48 wired-in adapters"
_TOTAL_CLAIM_RE = re.compile(r"enumerates all (\d+) wired-in adapters")


def _claimed(pattern: re.Pattern[str]) -> int:
    """Return the single integer *pattern* captures in the README."""
    matches = pattern.findall(README.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected exactly one README match for {pattern.pattern!r}, got {matches}"
    return int(matches[0])


def _install_matrix_rows() -> list[str]:
    """Return the data rows of the ``## Install matrix`` table.

    The table is the last one on the page; rows are collected from the
    ``## Install matrix`` heading onward, skipping the header row and the
    ``|---|---|`` separator.
    """
    lines = ADAPTER_INDEX.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "## Install matrix")
    except StopIteration:  # pragma: no cover - defensive
        pytest.fail(f"'## Install matrix' heading not found in {ADAPTER_INDEX}")

    rows = [line for line in lines[start:] if line.startswith("|")]
    assert rows, f"no table rows found under '## Install matrix' in {ADAPTER_INDEX}"
    # Drop the header row and the separator row.
    return [row for row in rows[2:] if set(row) - set("|- ")]


def test_readme_install_matrix_count_matches_the_table() -> None:
    """The README's install-command count equals the matrix row count."""
    assert _claimed(_MATRIX_CLAIM_RE) == len(_install_matrix_rows())


def test_readme_total_adapter_count_matches_the_registry() -> None:
    """The README's wired-in adapter count equals what the CLI enumerates."""
    assert _claimed(_TOTAL_CLAIM_RE) == len(_enumerate_rows())


def test_install_matrix_is_a_subset_claim_not_a_full_one() -> None:
    """The matrix must stay smaller than or equal to the enumerated set.

    A matrix larger than the registry means the table lists agents that no
    adapter drives - the inverse of the drift this module guards against.
    """
    assert len(_install_matrix_rows()) <= len(_enumerate_rows())
