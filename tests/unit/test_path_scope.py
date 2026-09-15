"""The path-scope matcher, one test per way it could quietly be wrong.

Two callers depend on this answer being the same on both sides: a volunteer
submission's diff against the project manifest's ``allowed_paths`` (#3869), and
the merge that accepts an agent's work against the credential's ``allowed_files``
(#3781). Every test below names a way the matcher could admit something it
should not, or refuse something everyone has.
"""

from __future__ import annotations

import pytest

from bernstein.core.path_scope import normalise_repo_path, paths_outside_scope, pattern_subsumes


def test_an_empty_scope_admits_every_path() -> None:
    """The load-bearing default on both surfaces.

    Every credential and every manifest that has never declared a scope carries
    an empty list. Reading that as "admit nothing" would refuse all existing
    work the first time the gate ran.
    """
    assert paths_outside_scope(["src/a.py", "/etc/passwd", "../x"], []) == ()


def test_a_path_no_pattern_admits_is_reported() -> None:
    """The whole point: something outside the scope comes back named."""
    assert paths_outside_scope(["src/a.py", "uv.lock"], ["src/**"]) == ("uv.lock",)


def test_a_double_star_segment_crosses_directories() -> None:
    """`src/**` has to mean the tree, or no realistic scope can be written."""
    outside = paths_outside_scope(["src/a.py", "src/deep/nested/b.py"], ["src/**"])
    assert outside == ()


def test_a_single_star_does_not_cross_a_directory_separator() -> None:
    """The `fnmatch` trap, and the reason this module exists.

    ``fnmatch("src/deep/b.py", "src/*")`` is true. A scope meant to admit the
    files directly under ``src/`` would silently admit everything beneath it,
    which is the difference between a boundary and a decoration.
    """
    assert paths_outside_scope(["src/a.py"], ["src/*"]) == ()
    assert paths_outside_scope(["src/deep/b.py"], ["src/*"]) == ("src/deep/b.py",)


def test_a_pattern_is_not_a_prefix() -> None:
    """`src` admits the path `src`, not the tree under it.

    This is the rule people expect to work the other way. Getting it wrong
    widens every scope silently, so it is pinned rather than assumed.
    """
    assert paths_outside_scope(["src/a.py"], ["src"]) == ("src/a.py",)
    assert paths_outside_scope(["src"], ["src"]) == ()


def test_a_question_mark_matches_one_character_and_never_a_separator() -> None:
    assert paths_outside_scope(["src/a.py"], ["src/?.py"]) == ()
    assert paths_outside_scope(["src/ab.py"], ["src/?.py"]) == ("src/ab.py",)
    assert paths_outside_scope(["src/a/py"], ["src/a?py"]) == ("src/a/py",)


def test_a_middle_double_star_matches_zero_intervening_segments() -> None:
    """`a/**/b` has to cover `a/b`, or every scope needs two patterns."""
    assert paths_outside_scope(["a/b", "a/x/b", "a/x/y/b"], ["a/**/b"]) == ()
    assert paths_outside_scope(["a/x/c"], ["a/**/b"]) == ("a/x/c",)


def test_a_bracket_is_a_literal_character_and_not_a_class() -> None:
    """No character classes, so a half-open bracket cannot rewrite a pattern.

    An unbalanced `[` in a stored scope would otherwise be a regex error or,
    worse, change what the rest of the pattern means.
    """
    assert paths_outside_scope(["src/[a].py"], ["src/[a].py"]) == ()
    assert paths_outside_scope(["src/a.py"], ["src/[a].py"]) == ("src/a.py",)
    # An unbalanced bracket is a literal too, not a crash.
    assert paths_outside_scope(["src/a.py"], ["src/[.py"]) == ("src/a.py",)


def test_an_empty_pattern_admits_nothing_rather_than_everything() -> None:
    """The fail-open direction is the dangerous one.

    A scope entry that cannot be read must not degrade into "no scope". Note
    this is a single unusable *entry*; an empty pattern *list* still means no
    restriction, which the first test pins.
    """
    assert paths_outside_scope(["src/a.py"], [""]) == ("src/a.py",)
    assert paths_outside_scope(["src/a.py"], ["", "src/**"]) == ()


def test_a_windows_separator_is_read_as_a_separator() -> None:
    """`git diff` prints `/`; a caller reading the working tree may not.

    Left unnormalised, `src\\a.py` matches no `src/**` pattern and a perfectly
    in-scope file is refused on one platform only.
    """
    assert paths_outside_scope(["src\\a.py"], ["src/**"]) == ()
    assert paths_outside_scope(["src/a.py"], ["src\\**"]) == ()


def test_a_leading_dot_slash_names_the_same_path() -> None:
    assert paths_outside_scope(["./src/a.py"], ["src/**"]) == ()
    assert normalise_repo_path("././src/a.py") == "src/a.py"


def test_the_report_keeps_input_order_and_does_not_repeat_a_path() -> None:
    """The refusal message is built from this, and a reader scans it.

    Two rows for one file reads as two problems, and an order that does not
    match the diff makes a long list hard to check off.
    """
    paths = ["z.txt", "src/a.py", "a.txt", "z.txt", "./z.txt"]
    assert paths_outside_scope(paths, ["src/**"]) == ("z.txt", "a.txt")


def test_one_admitting_pattern_is_enough() -> None:
    """Patterns are alternatives, not conjunctions."""
    assert paths_outside_scope(["docs/x.md", "src/a.py"], ["src/**", "docs/**"]) == ()


@pytest.mark.parametrize("escape", ["../etc/passwd", "/etc/passwd", "~/.ssh/id_rsa"])
def test_a_path_reaching_outside_the_checkout_is_not_admitted_by_a_subtree_scope(escape: str) -> None:
    """Validation refuses these shapes as *patterns*; they still arrive as paths.

    A scope of `src/**` must not admit one by accident on the way in - the
    patterns are anchored at the start, so a leading `/` or `..` cannot be
    skipped over.
    """
    assert paths_outside_scope([escape], ["src/**"]) == (normalise_repo_path(escape),)


def test_a_bare_double_star_admits_everything() -> None:
    """Stated rather than discovered: `**` is a scope that restricts nothing.

    It is the same outcome as an empty list, reached deliberately, and someone
    writing it should get what they asked for rather than an anchored surprise.
    """
    assert paths_outside_scope(["a", "a/b/c.py", "../x"], ["**"]) == ()


def test_repeated_double_stars_say_nothing_the_first_did_not() -> None:
    """`a/**/**/b` folds to `a/**/b` rather than matching nothing.

    Each `**` accounts for its own separator, so two adjacent ones would emit
    two - a pattern that matches nothing, which is the wrong way for a typo to
    fail: silently narrower rather than loudly wrong.
    """
    assert paths_outside_scope(["a/b", "a/x/y/b"], ["a/**/**/b"]) == ()


# ---------------------------------------------------------------------------
# pattern_subsumes (issue #5418): is one pattern's language a subset of
# another's, over the pattern grammar itself, not by trying sample paths.
# ---------------------------------------------------------------------------


def test_double_star_subsumes_a_narrower_double_star_subtree() -> None:
    """`src/**` covers everything `src/core/**` could ever match."""
    assert pattern_subsumes("src/**", "src/core/**")


def test_a_pattern_is_not_a_prefix_for_subsumption_either() -> None:
    """`src` admits only the path `src`, so it does not subsume `src/core`.

    The same rule :func:`paths_outside_scope` pins for matching applies to
    subsumption: a pattern is not a prefix.
    """
    assert not pattern_subsumes("src", "src/core")


def test_a_single_star_does_not_subsume_a_deeper_path() -> None:
    """`src/*` only ever matches two segments; it cannot cover a three-segment path."""
    assert not pattern_subsumes("src/*", "src/a/b")


def test_a_pattern_subsumes_itself() -> None:
    """Reflexivity: every pattern's language is a subset of its own."""
    for pattern in ("src/**", "src/*.py", "src", "**", "a/**/b", "src/*"):
        assert pattern_subsumes(pattern, pattern)


def test_bare_double_star_subsumes_everything() -> None:
    """`**` is the widest single pattern there is."""
    assert pattern_subsumes("**", "src/**")
    assert pattern_subsumes("**", "anything/at/all.py")


def test_a_narrower_double_star_does_not_subsume_the_wider_one() -> None:
    """Subsumption is not symmetric: the wider pattern does not fit inside the narrower one."""
    assert not pattern_subsumes("src/core/**", "src/**")


def test_single_star_subsumes_a_literal_within_the_same_segment() -> None:
    """`src/*` covers any single path segment under `src/`, including a literal one."""
    assert pattern_subsumes("src/*", "src/core")


def test_single_star_does_not_subsume_a_wildcard_it_cannot_bound() -> None:
    """`src/a*` cannot cover `src/*` -- the child can produce content the parent's literal prefix forbids."""
    assert not pattern_subsumes("src/a*", "src/*")


def test_question_mark_subsumes_a_single_literal_character() -> None:
    assert pattern_subsumes("src/?.py", "src/a.py")


def test_question_mark_does_not_subsume_a_star() -> None:
    """A child `*` can produce zero or many characters; a single `?` can never cover that."""
    assert not pattern_subsumes("src/?.py", "src/*.py")


def test_an_empty_pattern_subsumes_nothing() -> None:
    """An empty pattern admits nothing on either side of the comparison."""
    assert not pattern_subsumes("", "src/**")
    assert not pattern_subsumes("src/**", "")
