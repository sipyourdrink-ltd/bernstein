"""Scope resolution tests (issue #3752)."""

from __future__ import annotations

from bernstein.core.knowledge.conventions import ConventionReceipt, compute_rule_text_hash
from bernstein.core.quality.review_pipeline.scope import (
    compute_resolution_hash,
    resolve_scope,
)


def _receipt(receipt_id: str, subject_path: str, rule_text: str = "rule") -> ConventionReceipt:
    return ConventionReceipt(
        receipt_id=receipt_id,
        rule_text=rule_text,
        rule_text_hash=compute_rule_text_hash(rule_text),
        subject_path=subject_path,
        base_commit_sha="abc123",
    )


def test_same_inputs_same_hash_twice() -> None:
    r1 = _receipt("r1", "src/bernstein/core/security/**", "no tokens")
    changed = ["src/bernstein/core/security/x.py", "src/other/y.py"]
    s1 = resolve_scope(changed, [r1])
    s2 = resolve_scope(changed, [r1])
    assert compute_resolution_hash(s1) == compute_resolution_hash(s2)


def test_determinism_across_two_separate_calls() -> None:
    r1 = _receipt("r1", "src/a.py", "rule a")
    r2 = _receipt("r2", "src/b.py", "rule b")
    changed = ["src/a.py", "src/b.py", "src/c.py"]
    # Different input ordering should still yield same hash
    s_a = resolve_scope(list(reversed(changed)), [r2, r1])
    s_b = resolve_scope(changed, [r1, r2])
    assert compute_resolution_hash(s_a) == compute_resolution_hash(s_b)
    assert s_a.unscoped_paths == s_b.unscoped_paths
    assert [r.receipt_id for r in s_a.in_scope_receipts] == [r.receipt_id for r in s_b.in_scope_receipts]


def test_different_receipts_different_hash() -> None:
    changed = ["src/a.py"]
    r1 = _receipt("r1", "src/a.py", "rule one")
    r2 = _receipt("r2", "src/a.py", "rule two")
    h1 = compute_resolution_hash(resolve_scope(changed, [r1]))
    h2 = compute_resolution_hash(resolve_scope(changed, [r2]))
    assert h1 != h2


def test_unscoped_paths_contains_unmatched() -> None:
    r1 = _receipt("r1", "src/a.py", "rule a")
    changed = ["src/a.py", "src/b.py", "src/c.py"]
    scope = resolve_scope(changed, [r1])
    assert "src/b.py" in scope.unscoped_paths
    assert "src/c.py" in scope.unscoped_paths
    assert "src/a.py" not in scope.unscoped_paths


def test_glob_matching_works() -> None:
    r1 = _receipt("r1", "src/bernstein/core/security/**", "sec rule")
    changed = ["src/bernstein/core/security/x.py", "src/bernstein/core/other/y.py"]
    scope = resolve_scope(changed, [r1])
    assert len(scope.in_scope_receipts) == 1
    assert scope.in_scope_receipts[0].receipt_id == "r1"
    assert scope.matched_globs == ("src/bernstein/core/security/**",)
    assert "src/bernstein/core/other/y.py" in scope.unscoped_paths
    assert "src/bernstein/core/security/x.py" not in scope.unscoped_paths


def test_brace_glob_subject_path() -> None:
    r1 = _receipt("r1", "{src/a.py,src/b.py}", "brace rule")
    # Each alternative should match individually
    s_a = resolve_scope(["src/a.py"], [r1])
    assert len(s_a.in_scope_receipts) == 1
    s_b = resolve_scope(["src/b.py"], [r1])
    assert len(s_b.in_scope_receipts) == 1
    s_c = resolve_scope(["src/c.py"], [r1])
    assert len(s_c.in_scope_receipts) == 0
    assert "src/c.py" in s_c.unscoped_paths
    # Both together
    s_both = resolve_scope(["src/a.py", "src/b.py", "src/c.py"], [r1])
    assert len(s_both.in_scope_receipts) == 1
    assert "src/c.py" in s_both.unscoped_paths
    assert "src/a.py" not in s_both.unscoped_paths


def test_brace_glob_with_wildcard() -> None:
    r1 = _receipt("r1", "{src/a/**,src/b/**}", "brace wildcard")
    scope = resolve_scope(["src/a/x.py", "src/b/y.py", "src/c/z.py"], [r1])
    assert len(scope.in_scope_receipts) == 1
    assert "src/c/z.py" in scope.unscoped_paths
    assert "src/a/x.py" not in scope.unscoped_paths


def test_dedupes_receipts_and_orders_deterministically() -> None:
    r1 = _receipt("r1", "src/b.py", "rule b")
    r2 = _receipt("r2", "src/a.py", "rule a")
    # Pass receipts in reverse order, and duplicate r1
    scope = resolve_scope(["src/a.py", "src/b.py"], [r1, r2, r1])
    ids = [r.receipt_id for r in scope.in_scope_receipts]
    # Ordered by (subject_path, rule_text_hash, receipt_id) -> r2 (src/a.py) before r1 (src/b.py)
    assert ids == ["r2", "r1"]


def test_no_receipts_all_unscoped() -> None:
    scope = resolve_scope(["src/a.py", "src/b.py"], [])
    assert scope.in_scope_receipts == ()
    assert scope.matched_globs == ()
    assert scope.unscoped_paths == ("src/a.py", "src/b.py")
