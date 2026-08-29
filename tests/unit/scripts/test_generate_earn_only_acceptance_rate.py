#!/usr/bin/env python3
"""Tests for generate-earn-only-acceptance-rate.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TEST_MODE"] = "true"

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate-earn-only-acceptance-rate.py"
_spec = importlib.util.spec_from_file_location("generate_earn_only_acceptance_rate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

extract_bundle_references = _mod.extract_bundle_references  # type: ignore[attr-defined]
is_reverted_pr = _mod.is_reverted_pr  # type: ignore[attr-defined]
process_prs = _mod.process_prs  # type: ignore[attr-defined]

_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "volunteer" / "test_prs.json"

ALICE = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
BOB = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
CHARLIE = "9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba"
DIANA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
UNKNOWN = "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _load_fixture_prs() -> list[dict[str, object]]:
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)["prs"]  # type: ignore[no-any-return]


def test_counts_from_fixture_pr_set() -> None:
    """Submitted/verified/merged counts match generator output on fixture."""
    prs = _load_fixture_prs()
    counts = process_prs(prs)  # type: ignore[arg-type]

    assert ALICE in counts
    assert counts[ALICE] == {"submitted": 2, "verified": 2, "merged": 2, "reverted": 0}

    assert BOB in counts
    assert counts[BOB] == {"submitted": 2, "verified": 2, "merged": 2, "reverted": 1}

    assert CHARLIE in counts
    assert counts[CHARLIE] == {"submitted": 1, "verified": 1, "merged": 1, "reverted": 0}

    assert DIANA in counts
    assert counts[DIANA] == {"submitted": 1, "verified": 1, "merged": 1, "reverted": 0}


def test_reverted_pr_decrement() -> None:
    """A PR reverted by a later merged PR increments reverted count."""
    prs = _load_fixture_prs()
    counts = process_prs(prs)  # type: ignore[arg-type]
    assert counts[BOB]["submitted"] == 2
    assert counts[BOB]["merged"] == 2
    assert counts[BOB]["reverted"] == 1
    assert counts[BOB]["verified"] == 2


def test_unknown_worker_key_isolation() -> None:
    """PRs without a recognisable worker_keyid do not contribute."""
    prs: list[dict[str, object]] = [
        {
            "number": 2001,
            "title": "feat: test contribution",
            "body": "No key here",
            "merged_at": "2026-07-15T10:30:00Z",
            "html_url": "http://example.com",
            "user": "someone",
            "labels": [],
            "state": "closed",
        },
        {
            "number": 2002,
            "title": "feat: test contribution 2",
            "body": f"random text worker_keyid: {UNKNOWN}",
            "merged_at": "2026-07-16T10:30:00Z",
            "html_url": "http://example.com",
            "user": "other",
            "labels": [],
            "state": "closed",
        },
    ]
    counts = process_prs(prs)  # type: ignore[arg-type]
    assert len(counts) == 1
    assert UNKNOWN in counts
    assert counts[UNKNOWN] == {"submitted": 1, "verified": 1, "merged": 1, "reverted": 0}


def test_determinism_across_runs() -> None:
    """Running process_prs twice yields identical output."""
    prs = _load_fixture_prs()
    counts_1 = process_prs(prs)  # type: ignore[arg-type]
    counts_2 = process_prs(prs)  # type: ignore[arg-type]
    assert counts_1 == counts_2


def test_extract_bundle_references_from_body() -> None:
    """worker_keyid patterns are extracted correctly."""
    body = f"worker_keyid: {ALICE}"
    refs = extract_bundle_references(body)
    assert len(refs) == 1
    assert refs[0]["type"] == "worker_keyid"
    assert refs[0]["value"] == ALICE


def test_extract_bundle_references_no_body() -> None:
    """Empty body yields no references."""
    assert extract_bundle_references(None) == []
    assert extract_bundle_references("") == []


def test_is_reverted_pr_detects_reference() -> None:
    """PR titled revert mentioning #N is reverted."""
    original: dict[str, object] = {
        "number": 1,
        "title": "original",
        "body": "",
        "merged_at": "2026-07-15T10:30:00Z",
    }
    reverted: dict[str, object] = {
        "number": 2,
        "title": "Revert original",
        "body": "reverts #1",
        "merged_at": "2026-07-16T10:30:00Z",
    }
    assert is_reverted_pr(original, [original, reverted]) is True  # type: ignore[arg-type]


def test_is_reverted_pr_no_reference() -> None:
    """Revert title without #N reference is not a revert."""
    pr1: dict[str, object] = {"number": 1, "title": "original", "body": "", "merged_at": "2026-07-15T10:30:00Z"}
    pr2: dict[str, object] = {
        "number": 2,
        "title": "Revert unrelated",
        "body": "reverts #999",
        "merged_at": "2026-07-16T10:30:00Z",
    }
    assert is_reverted_pr(pr1, [pr1, pr2]) is False  # type: ignore[arg-type]


def test_full_generator_output_matches_expected_structure() -> None:
    """Generator produces deterministic JSON with expected worker counts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["TEST_MODE"] = "true"
        sys.argv = [
            "generate-earn-only-acceptance-rate.py",
            "--month",
            "2026-07",
            "--repo",
            "test/test",
            "--output",
            tmpdir,
        ]
        rc: int = _mod.main()  # type: ignore[attr-defined]
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
        assert len(workers) >= 2
        assert ALICE in workers
