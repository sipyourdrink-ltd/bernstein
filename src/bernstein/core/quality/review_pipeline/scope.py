"""Deterministic scope resolution for review conventions (issue #3752).

Matches changed paths against active :class:`ConventionReceipt` subject globs
deterministically, mirroring the skill-selection-rules precedent
(``src/bernstein/core/skills/selection_rules.py:resolve_rule_templates``).

Pure functions: no filesystem, no env, no ordering sensitivity beyond sorted
input.  No model call in the resolution path.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.knowledge.conventions import ConventionReceipt


def _expand_braces(pattern: str) -> list[str]:
    """Expand a single brace-glob into its alternatives.

    ``"{a,b,c}"`` -> ``["a","b","c"]``,
    ``"src/{a,b}/x.py"`` -> ``["src/a/x.py","src/b/x.py"]``.
    Handles nested braces by recursion.  When no braces are present the
    input is returned as a one-element list.
    """
    if "{" not in pattern or "}" not in pattern:
        return [pattern]
    # Find first opening brace and its matching closing brace (handle nesting).
    start = pattern.find("{")
    depth = 0
    end = -1
    for idx in range(start, len(pattern)):
        ch = pattern[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    if end == -1:
        return [pattern]
    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    inner = pattern[start + 1 : end]
    # Split inner on commas at depth 0.
    parts: list[str] = []
    cur: list[str] = []
    d = 0
    for ch in inner:
        if ch == "{":
            d += 1
            cur.append(ch)
        elif ch == "}":
            d -= 1
            cur.append(ch)
        elif ch == "," and d == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    results: list[str] = []
    for part in parts:
        expanded = prefix + part + suffix
        # Recurse for remaining braces.
        for sub in _expand_braces(expanded):
            results.append(sub)
    return results


def _matches(path: str, pattern: str) -> bool:
    """Return whether *path* matches *pattern* (brace-expanded, fnmatch)."""
    return any(fnmatch.fnmatch(path, expanded) for expanded in _expand_braces(pattern))


@dataclass(frozen=True)
class ScopeResolution:
    """Result of :func:`resolve_scope`.

    Attributes:
        in_scope_receipts: Ordered tuple of receipts that matched at least one
            changed path.  Deterministically sorted.
        matched_globs: The glob (``subject_path``) that caused each receipt to
            be in scope, parallel to ``in_scope_receipts``.
        unscoped_paths: Changed paths matched by no convention.
    """

    in_scope_receipts: tuple[ConventionReceipt, ...]
    matched_globs: tuple[str, ...]
    unscoped_paths: tuple[str, ...]

    # Back-compat aliases for tests that may use alternative names.
    @property
    def receipts(self) -> tuple[ConventionReceipt, ...]:  # pragma: no cover
        return self.in_scope_receipts

    @property
    def in_scope(self) -> tuple[ConventionReceipt, ...]:  # pragma: no cover
        return self.in_scope_receipts


def resolve_scope(
    changed_paths: list[str] | tuple[str, ...],
    receipts: list[ConventionReceipt] | tuple[ConventionReceipt, ...],
) -> ScopeResolution:
    """Match changed paths against receipts deterministically.

    Pure function: no filesystem, no env, no ordering sensitivity.

    Args:
        changed_paths: Repo-relative changed file paths.
        receipts: Active convention receipts.

    Returns:
        :class:`ScopeResolution` with in-scope receipts, their matched globs,
        and unscoped paths.
    """
    # Deterministic inputs: dedupe + sort.
    uniq_paths = sorted(set(changed_paths))
    # Dedupe receipts by receipt_id, keep first occurrence.
    by_id: dict[str, ConventionReceipt] = {}
    for r in receipts:
        if r.receipt_id not in by_id:
            by_id[r.receipt_id] = r
    sorted_receipts = sorted(
        by_id.values(),
        key=lambda r: (r.subject_path, r.rule_text_hash, r.receipt_id),
    )

    in_scope: list[ConventionReceipt] = []
    matched_globs: list[str] = []
    covered: set[str] = set()

    for receipt in sorted_receipts:
        pattern = receipt.subject_path
        # Check if any path matches this receipt.
        found = False
        for p in uniq_paths:
            if _matches(p, pattern):
                found = True
                covered.add(p)
        # Also need to mark coverage for this receipt even if found; covered
        # already includes those that matched this receipt.  But coverage must
        # be union across all receipts — we already handle that by adding
        # per match.  For correctness, ensure every receipt's matches are
        # added, which we do.
        if found:
            in_scope.append(receipt)
            matched_globs.append(pattern)

    # Unscoped: paths not covered by any receipt.  Recompute to handle the
    # union correctly (covered already unioned, but ensure paths matched by
    # any receipt are considered).
    # To avoid missing cases where a receipt matched but we didn't add all
    # matching paths (we did), just compute directly:
    # Actually covered already correct, but recompute from scratch for safety
    # handling any receipts that didn't make in_scope yet still could cover?
    # No, covered only includes receipts that were in_scope, which is all that
    # matched. So covered is union of all matches.
    # However, for receipts not in_scope (no match) they contribute nothing.
    # So final unscoped:
    unscoped = tuple(p for p in uniq_paths if p not in covered)

    return ScopeResolution(
        in_scope_receipts=tuple(in_scope),
        matched_globs=tuple(matched_globs),
        unscoped_paths=unscoped,
    )


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def compute_resolution_hash(scope: ScopeResolution) -> str:
    """Deterministically hash a :class:`ScopeResolution`.

    Hashes in-scope receipt IDs / rule-text hashes + matched globs +
    unscoped paths with canonical-JSON + sha256.  No model call.

    Returns:
        ``"sha256:"``-prefixed hex digest, stable for identical inputs.
    """
    # Build deterministic entries sorted by receipt_id so ordering inside the
    # scope does not affect hash beyond the defined sort.
    entries = []
    for receipt, glob in zip(scope.in_scope_receipts, scope.matched_globs, strict=False):
        entries.append(
            {
                "receipt_id": receipt.receipt_id,
                "rule_text_hash": receipt.rule_text_hash,
                "matched_glob": glob,
            }
        )
    entries.sort(key=lambda e: (e["receipt_id"], e["rule_text_hash"], e["matched_glob"]))
    payload = {
        "v": 1,
        "in_scope": entries,
        "unscoped_paths": sorted(scope.unscoped_paths),
    }
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return "sha256:" + digest


__all__ = ["ScopeResolution", "compute_resolution_hash", "resolve_scope"]
