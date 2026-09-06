"""Containment between two repository-relative globs, and what it lets a scope do.

A credential's ``allowed_files`` is a glob set read by
:mod:`bernstein.core.path_scope`, and attenuating one asks a question the
matcher alone cannot answer: not "does this parent admit that path" but "does
this parent admit every path that child admits". Without it a glob scope could
not be narrowed at all -- a parent holding ``src/**`` was refused when it tried
to mint a child holding ``src/core/**`` (#5418).

Every test below names one way the relation could quietly become string
containment or ancestry, either of which would let a child reach files its
parent never held.
"""

from __future__ import annotations

import itertools

import pytest

from bernstein.core.identity.agent_jwt import _all_patterns_covered_by
from bernstein.core.path_scope import paths_outside_scope, pattern_subsumes
from bernstein.core.security.capability_tokens import globs_narrow, prefixes_narrow

# ---------------------------------------------------------------------------
# The relation between two patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outer", "inner"),
    [
        ("src/**", "src/core/**"),
        ("src/**", "src/core/thing.py"),
        ("src/**", "src"),
        ("src/**", "src/*"),
        ("src/*", "src/a"),
        ("**", "anything/at/all"),
        ("a/**/b", "a/x/b"),
        ("a/?", "a/x"),
        ("a*", "ab*"),
    ],
)
def test_a_pattern_subsumes_what_it_admits(outer: str, inner: str) -> None:
    """No path exists that `inner` admits and `outer` does not."""
    assert pattern_subsumes(outer, inner) is True


@pytest.mark.parametrize(
    ("outer", "inner"),
    [
        ("src", "src/core"),
        ("src/*", "src/a/b"),
        ("src/core/**", "src/**"),
        ("a/?", "a/*"),
        ("ab*", "a*"),
        ("a/**/b", "a/x/c"),
    ],
)
def test_a_pattern_does_not_subsume_what_it_cannot_admit(outer: str, inner: str) -> None:
    assert pattern_subsumes(outer, inner) is False


def test_a_pattern_is_not_a_prefix_which_is_why_ancestry_is_the_wrong_relation() -> None:
    """`src` admits the path `src` and nothing under it, so it subsumes neither.

    The prefix primitive answers this the other way, correctly, for the signed
    capability tokens it serves. Reading a glob with it would report `src` as
    covering `src/core` -- a narrowing that never happened.
    """
    assert paths_outside_scope(["src/core"], ["src"]) == ("src/core",)
    assert pattern_subsumes("src", "src/core") is False
    assert prefixes_narrow(frozenset({"src/core"}), frozenset({"src"})) is True


def test_a_single_star_does_not_reach_across_a_separator() -> None:
    """The `fnmatch` trap, restated for the containment question."""
    assert pattern_subsumes("src/*", "src/a/b") is False
    assert pattern_subsumes("src/**", "src/a/b") is True


def test_subsumption_agrees_with_the_matcher_on_every_pattern_pair() -> None:
    """Soundness, checked rather than asserted.

    For every pair the relation calls subsumption, no sampled path may be
    admitted by the inner pattern and refused by the outer one. This is the
    property the whole module rests on: a false positive here widens a scope.
    """
    segments = ("a", "b", "ab", "*", "?", "**", "a*")
    patterns = sorted({"/".join(c) for n in (1, 2) for c in itertools.product(segments, repeat=n)})
    paths = sorted({"/".join(c) for n in (1, 2, 3) for c in itertools.product(("a", "b", "ab", "x"), repeat=n)})

    def admits(pattern: str, path: str) -> bool:
        return not paths_outside_scope((path,), (pattern,))

    unsound = [
        (outer, inner, path)
        for outer in patterns
        for inner in patterns
        if pattern_subsumes(outer, inner)
        for path in paths
        if admits(inner, path) and not admits(outer, path)
    ]
    assert unsound == []


# ---------------------------------------------------------------------------
# The relation between two sets
# ---------------------------------------------------------------------------


def test_none_is_the_widest_set_and_is_narrowed_only_by_none() -> None:
    """The axis follows the capability-token convention: `None` is unconstrained."""
    narrow = frozenset({"src/**"})
    assert globs_narrow(narrow, None) is True
    assert globs_narrow(None, None) is True
    assert globs_narrow(None, narrow) is False


def test_every_child_pattern_must_be_subsumed_not_merely_most() -> None:
    """One pattern outside the parent's scope widens the whole set."""
    assert globs_narrow(frozenset({"src/a/**", "docs/**"}), frozenset({"src/**"})) is False
    assert globs_narrow(frozenset({"src/a/**", "src/b/**"}), frozenset({"src/**"})) is True


def test_a_child_covered_only_by_two_parents_together_is_reported_as_widening() -> None:
    """The undecided case answers in the direction that cannot overstate a grant.

    `a/?` is admitted entirely by `{a/x, a/y}` only if the alphabet were those
    two letters. Refusing keeps a scope from recording a narrowing whose proof
    it does not actually have.
    """
    assert globs_narrow(frozenset({"a/?"}), frozenset({"a/x", "a/y"})) is False


# ---------------------------------------------------------------------------
# What it lets a credential do
# ---------------------------------------------------------------------------


def test_a_glob_scope_can_now_be_narrowed_to_a_sub_glob() -> None:
    """Fails before this change: a glob child was refused whatever it said.

    A parent holding the tree could hand out a single file but not a subtree,
    so `allowed_files` could be dropped or kept and never actually attenuated.
    """
    assert _all_patterns_covered_by({"src/core/**"}, {"src/**"}) is True


def test_a_glob_child_reaching_outside_the_parent_is_still_refused() -> None:
    """The direction that must never open: the whole point of the check."""
    assert _all_patterns_covered_by({"docs/**"}, {"src/**"}) is False
    assert _all_patterns_covered_by({"src/**"}, {"src/core/**"}) is False


def test_a_literal_child_is_still_decided_by_the_matcher() -> None:
    """The path the change does not touch, pinned so it cannot drift."""
    assert _all_patterns_covered_by({"src/a.py"}, {"src/**"}) is True
    assert _all_patterns_covered_by({"src/core"}, {"src"}) is False


def test_a_child_needing_two_parent_patterns_together_is_refused() -> None:
    """Refusing is the direction that cannot widen a credential's file scope."""
    assert _all_patterns_covered_by({"src/?"}, {"src/a", "src/b"}) is False
