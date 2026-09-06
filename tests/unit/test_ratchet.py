"""Tests for the two-way ratchet snapshot allowlist assertion helper (#5552, #5503)."""

from __future__ import annotations

import pytest

from tests.unit._ratchet import (
    assert_ratchet_matches,
    format_snapshot_snippet,
)


def test_clean_ratchet_passes_silently() -> None:
    """When current matches baseline exactly, no assertion is raised."""
    current = {"mod_a", "mod_b"}
    baseline = {"mod_a", "mod_b"}
    assert_ratchet_matches(current, baseline, subject="test module list")


def test_both_directions_of_drift_reported_in_single_failure() -> None:
    """Both newly appeared entries and removed/stale entries must be reported together.

    Stopping at the first assertion hides half the work and guarantees a second red.
    """
    current = {"mod_a", "mod_c"}
    baseline = {"mod_a", "mod_b"}

    with pytest.raises(AssertionError) as exc_info:
        assert_ratchet_matches(
            current,
            baseline,
            subject="core/tokens/ orphan allowlist",
            constant_name="KNOWN_ORPHANS",
            wire_hint="Wire each one to a consumer or delete the module.",
        )

    msg = str(exc_info.value)
    # Reports both directions
    assert "New entries introduced" in msg
    assert "+ mod_c" in msg
    assert "Entries in KNOWN_ORPHANS that no longer exist" in msg
    assert "- mod_b" in msg
    # Includes copy-pasteable snippet
    assert 'KNOWN_ORPHANS = frozenset({\n    "mod_a",\n    "mod_c",\n})' in msg
    assert "Wire each one to a consumer" in msg


def test_git_attribution_distinguishes_branch_changes_from_main_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a file was NOT touched by the branch, the failure explains the baseline is stale from main."""
    import tests.unit._ratchet as _ratchet_mod

    # Simulate git diff reporting only 'src/bernstein/core/tokens/branch_mod.py' changed on this branch
    monkeypatch.setattr(
        _ratchet_mod,
        "_branch_changed_files",
        lambda: {"src/bernstein/core/tokens/branch_mod.py"},
    )

    current = {"branch_mod", "main_mod"}
    baseline = set()
    file_mapping = {
        "branch_mod": "src/bernstein/core/tokens/branch_mod.py",
        "main_mod": "src/bernstein/core/tokens/main_mod.py",
    }

    with pytest.raises(AssertionError) as exc_info:
        assert_ratchet_matches(
            current,
            baseline,
            subject="token orphans",
            constant_name="KNOWN_ORPHANS",
            file_mapping=file_mapping,
        )

    msg = str(exc_info.value)
    assert "[Branch Changes] New entries introduced by this branch (1):" in msg
    assert "+ branch_mod" in msg
    assert "[Baseline Stale] Entries present on main but missing in snapshot (1):" in msg
    assert "+ main_mod (from main: file not touched by this branch)" in msg
    assert "The branch is not at fault. Rebase onto main" in msg


def test_format_snapshot_snippet_empty() -> None:
    assert format_snapshot_snippet("EMPTY_SET", set()) == "EMPTY_SET = frozenset()"


def test_format_snapshot_snippet_sorted() -> None:
    snippet = format_snapshot_snippet("TEST_SET", ["z_mod", "a_mod"])
    expected = 'TEST_SET = frozenset({\n    "a_mod",\n    "z_mod",\n})'
    assert snippet == expected
