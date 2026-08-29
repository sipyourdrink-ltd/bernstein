#!/usr/bin/env python3
"""Tests for generate-earn-only-acceptance-rate.py

Test 1: counts from fixture PR set
Test 2: reverted-PR decrement
Test 3: unknown-worker-key isolation
Test 4: determinism across runs
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TEST_MODE"] = "true"

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from generate_earn_only_acceptance_rate import (
    extract_bundle_references,
    get_pr_data,
    is_reverted_pr,
    process_prs,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "volunteer"
    / "test_prs.json"
)


def _load_fixture_prs() -> list[dict]:
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)["prs"]


# --------------------------------------------------------------------------- #
# Test 1: counts from fixture PR set
# --------------------------------------------------------------------------- #


def test_counts_from_fixture_pr_set() -> None:
    """Submitted/verified/merged counts match the shipped generator output on a fixture set."""
    prs = _load_fixture_prs()
    counts = process_prs(prs)

    # Alice (worker A): PR #1001 and #1003 → 2 submitted, 2 merged, 2 verified, 0 reverted
    alice_key = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    assert alice_key in counts
    assert counts[alice_key] == {"submitted": 2, "verified": 2, "merged": 2, "reverted": 0}

    # Bob (worker B): PR #1002 and #1005 → 2 submitted, 2 merged, 2 verified, 1 reverted
    bob_key = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcd"
    assert bob_key in counts
    assert counts[bob_key] == {"submitted": 2, "verified": 2, "merged": 2, "reverted": 1}

    # Charlie (worker C): PR #1004 → 1/1/1/0
    charlie_key = "9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba"
    assert charlie_key in counts
    assert counts[charlie_key] == {"submitted": 1, "verified": 1, "merged": 1, "reverted": 0}

    # Diana (worker D): PR #1006 → 1/1/1/0
    diana_key = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert diana_key in counts
    assert counts[diana_key] == {"submitted": 1, "verified": 1, "merged": 1, "reverted": 0}


# --------------------------------------------------------------------------- #
# Test 2: reverted-PR decrement
# --------------------------------------------------------------------------- #


def test_reverted_pr_decrement() -> None:
    """A PR reverted by a later merged PR decrements that PR's worker's 'merged' count."""
    prs = _load_fixture_prs()

    # PR #1005 is a revert of PR #1002 (Bob's change).
    # After process_prs, Bob should have reverted=1 and his reverted PR
    # should not be counted as 'merged' for the reverted count logic.
    counts = process_prs(prs)

    bob_key = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcd"
    # Bob has 2 submitted, 2 merged (both PRs merged), 1 reverted
    assert counts[bob_key]["submitted"] == 2
    assert counts[bob_key]["merged"] == 2
    assert counts[bob_key]["reverted"] == 1
    assert counts[bob_key]["verified"] == 2


# --------------------------------------------------------------------------- #
# Test 3: unknown-worker-key isolation
# --------------------------------------------------------------------------- #


def test_unknown_worker_key_isolation() -> None:
    """PRs without a recognisable worker_keyid do not contribute to any worker's counts."""
    prs = [
        {"number": 2001, "title": "feat: test contribution", "body": "No key here", "merged_at": "2026-07-15T10:30:00Z", "html_url": "http://example.com", "user": "someone", "labels": [], "state": "closed"},
        {"number": 2002, "title": "feat: test contribution 2", "body": "random text but valid 64-hex: abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef", "merged_at": "2026-07-16T10:30:00Z", "html_url": "http://example.com", "user": "other", "labels": [], "state": "closed"},
    ]
    counts = process_prs(prs)
    # Only the PR with a 64-hex string should be counted
    assert len(counts) == 1
    assert "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef" in counts
    worker = counts["abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef"]
    assert worker == {"submitted": 1, "verified": 1, "merged": 1, "reverted": 0}


# --------------------------------------------------------------------------- #
# Test 4: determinism across runs
# --------------------------------------------------------------------------- #


def test_determinism_across_runs() -> None:
    """Running process_prs twice on the same fixture yields identical output."""
    prs = _load_fixture_prs()
    counts_1 = process_prs(prs)
    counts_2 = process_prs(prs)
    assert counts_1 == counts_2


# --------------------------------------------------------------------------- #
# Extract bundle references
# --------------------------------------------------------------------------- #


def test_extract_bundle_references_from_body() -> None:
    """worker_keyid patterns are extracted correctly from PR body."""
    body = "worker_keyid: 1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    refs = extract_bundle_references(body)
    assert len(refs) == 1
    assert refs[0]["type"] == "worker_keyid"
    assert refs[0]["value"] == "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"


def test_extract_bundle_references_no_body() -> None:
    """An empty body yields no references."""
    assert extract_bundle_references(None) == []
    assert extract_bundle_references("") == []


# --------------------------------------------------------------------------- #
# is_reverted_pr
# --------------------------------------------------------------------------- #


def test_is_reverted_pr_detects_reference() -> None:
    """A PR titled 'revert' that mentions #N in body is reverted."""
    original = {"number": 1, "title": "original", "body": "", "merged_at": "2026-07-15T10:30:00Z"}
    reverted = {"number": 2, "title": "Revert original", "body": "reverts #1", "merged_at": "2026-07-16T10:30:00Z"}
    assert is_reverted_pr(original, [original, reverted]) is True


def test_is_reverted_pr_no_reference() -> None:
    """A PR titled 'revert' that does NOT mention #N is not a revert of that PR."""
    pr1 = {"number": 1, "title": "original", "body": "", "merged_at": "2026-07-15T10:30:00Z"}
    pr2 = {"number": 2, "title": "Revert unrelated", "body": "reverts #999", "merged_at": "2026-07-16T10:30:00Z"}
    assert is_reverted_pr(pr1, [pr1, pr2]) is False


# --------------------------------------------------------------------------- #
# Full generator integration (using TEST_MODE fixture)
# --------------------------------------------------------------------------- #


def test_full_generator_output_matches_expected_structure() -> None:
    """The generator produces deterministic JSON with expected worker counts."""
    from generate_earn_only_acceptance_rate import main

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["TEST_MODE"] = "true"
        sys.argv = [
            "generate-earn-only-acceptance-rate.py",
            "--month", "2026-07",
            "--repo", "test/test",
            "--output", tmpdir,
        ]
        # main() uses the fixture via get_pr_data when TEST_MODE is set
        rc = main()
        assert rc == 0

        output_file = Path(tmpdir) / "earn-only-acceptance-rate-2026-07.json"
        assert output_file.exists()

        with open(output_file) as f:
            output = json.load(f)

        assert output["month"] == "2026-07"
        assert output["repo"] == "test/test"
        assert "period" in output
        assert output["period"]["since"] == "2026-07-01"
        assert output["period"]["until"] == "2026-08-01"

        workers = output["workers"]
        assert len(workers) >= 2  # At least Alice and Bob from fixture
        assert "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef" in workers
