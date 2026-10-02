"""Guard the adapter counts the README states against the code they describe.

The README's "supported agents" paragraph makes three countable claims:

1. ``docs/adapters/index.md`` carries install commands for N adapters.
2. ``bernstein integrations list`` enumerates M wired-in adapters.
3. P of those wired-in adapters are selectable agent adapters.

Both the README's intro paragraph and its at-a-glance bullet restate claim 3
elsewhere on the page (see below), and the MkDocs Home page (``docs/index.md``)
restates the same shape of claim again in its own front matter and a feature
bullet - #6060 was about exactly this class of drift, one restatement at a
time.

All of these numbers drift silently when an adapter is added or the install
matrix gains a row. These tests recompute each number from its source - the
markdown table for N, ``integrations_cmd._enumerate_rows()`` for M and the
total wired-in count, ``registry.selectable_adapter_names()`` for the
selectable count - so a stale count fails here instead of shipping as a claim
the code cannot back.

When a count legitimately changes, update the README/``docs/index.md``
sentence and these tests will pass again.

Translated pages
----------------
The same numbers are repeated on all 23 pages under ``docs/i18n/``, and
nothing used to check them: two adapters once landed with the English counts
updated and every translation left behind, and the whole tree stayed green.
``bernstein readme-l10n verify`` does not close that gap on purpose - it binds
a hash over the section's *structure* (block counts, code fences, the English
source hash), not the digits inside it, so a translation whose numbers are
stale binds cleanly and reports OK. Structure and digits are two different
properties; this module owns the second.

Two decisions are encoded here.

**Coverage is strict.** Every ``docs/i18n/README.*.md`` must expose a
``supported agents`` binding and must carry every English count. A page that
cannot be located is a failure, not a skip - a page silently dropping out of
coverage is exactly the bug this guard exists to prevent.

**The assertion is presence of each English value**, not per-claim role
matching. Each of the three numbers English states must appear somewhere in
the translated section. The stricter reading - "each specific claim matches its
own English counterpart" - would need a way to tell the three numbers apart in
23 languages whose word order differs from English, and the English claim
regexes match none of the translated pages (the prose is translated). Presence
catches the drift that actually happens: a stale number means the English value
is absent, which fails and names the page. It deliberately tolerates a
legitimate extra integer - ``README.ja.md`` writes the digit ``2`` where
English writes the word "two" - so an exact set comparison is not usable.

Never run ``bernstein readme-l10n sync`` to make a failure here go away: sync
rebinds structural hashes and does not translate, so it would hide a stale
number rather than fix it.

**Known gap.** The intro paragraph's, at-a-glance bullet's and
``docs/index.md``'s counts are checked only on their English source pages,
not against the 23 translated READMEs (#6167 review). The translated-page
check above anchors on the localized "supported agents" l10n binding, and
these restatements live outside that section with no binding of their own
to anchor a per-page lookup on. A registry bump therefore still forces only
the English sentences to move; the translated copies of these three
restatements (unlike claims 1-3 above) can drift again with the whole suite
green. Left as a follow-up rather than widened here: covering them needs a
presence check across the whole translated page rather than one located
section, which trades away some of the specificity the strict, per-section
check above relies on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bernstein.cli.commands.integrations_cmd import _enumerate_rows
from bernstein.core.knowledge.readme_l10n import _find_translated_section

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
ADAPTER_INDEX = REPO_ROOT / "docs" / "adapters" / "index.md"
I18N_DIR = REPO_ROOT / "docs" / "i18n"

#: The MkDocs Home page. It restates the same "N more"-style claim the
#: README intro makes (in its front-matter ``description``, the page's SEO
#: meta text) and a separate "M CLI adapters" feature bullet - #6060 was
#: about a stale count exactly like these, and merging a fix that leaves
#: this page's copies stale would close the issue while shipping the same
#: defect on the site's own front page (#6167 review).
DOCS_INDEX = REPO_ROOT / "docs" / "index.md"

#: Translated front pages, sorted so parametrised ids are stable. Globbed
#: rather than listed, so a new language is covered the moment its file lands.
TRANSLATED_READMES = sorted(I18N_DIR.glob("README.*.md"))

#: The English heading whose translated counterpart carries the counts. The
#: translated heading is translated prose, so pages are located by their l10n
#: binding comment instead - see ``_find_translated_section``.
EN_SECTION_HEADING = "supported agents"

# "carries install commands for 29 of them"
_MATRIX_CLAIM_RE = re.compile(r"carries install commands for (\d+) of them")
# "enumerates all 51 wired-in integrations"
_TOTAL_CLAIM_RE = re.compile(r"enumerates all (\d+) wired-in integrations")
# "49 of them are selectable agent adapters"
_SELECTABLE_CLAIM_RE = re.compile(r"(\d+) of them are selectable agent adapters")

#: Every countable claim, with the label a failure message names it by.
_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("install-matrix", _MATRIX_CLAIM_RE),
    ("wired-in", _TOTAL_CLAIM_RE),
    ("selectable", _SELECTABLE_CLAIM_RE),
)

# "CLI coding agents work out of the box (Claude Code, Codex, Gemini CLI, and 49 more)"
_INTRO_CLAIM_RE = re.compile(r"Claude Code, Codex, Gemini CLI, and (\d+) more\)")
# "**Broad and local.** 52 selectable CLI agent adapters, including a generic `--prompt` wrapper"
_AT_A_GLANCE_CLAIM_RE = re.compile(r"(\d+) selectable CLI agent adapters, including a generic")

# These two restate the "supported agents" section's selectable count earlier
# on the page (the intro paragraph and the at-a-glance bullet) - the exact
# claims issue #6060 was filed against. They are intentionally not added to
# ``_CLAIMS``: that tuple also drives the translated-page check, which looks
# for claims inside the localized "supported agents" section, and these two
# live outside it.
#
# Neither sentence's number is the raw selectable count, and asserting
# otherwise would pin an overclaim rather than guard against one (#6167
# review): the intro names three adapters before "and N more", and those
# three are themselves members of the selectable set, so N has to be the
# selectable count *minus* the three named ones, or "3 + N" overstates the
# total by 3. The at-a-glance bullet's own wording says "including a
# generic wrapper" rather than "plus" for the same reason: ``generic`` is
# already one of the 52 selectable names, so "plus" would double-count it.

#: The three adapters the intro paragraph names before "and N more" -- named
#: so the offset the intro test subtracts cannot drift out of sync with the
#: sentence silently if the enumerated names ever change.
_NAMED_IN_INTRO: tuple[str, ...] = ("claude", "codex", "gemini")

# docs/index.md's front-matter description: "Run Claude Code, Codex, Gemini
# CLI, and 49 more behind one governance surface"
_DOCS_INDEX_DESCRIPTION_CLAIM_RE = re.compile(r"Claude Code, Codex, Gemini CLI, and (\d+) more behind")
# docs/index.md's "Any agent, any model" feature bullet: "52 CLI adapters:
# Claude Code, Codex, ..."
_DOCS_INDEX_FEATURE_CLAIM_RE = re.compile(r"(\d+) CLI adapters: Claude Code, Codex")

#: A standalone integer. ``\b`` is unusable here: Python's word boundary is
#: Unicode-aware and a Bengali letter is a word character, so ``\b52\b`` does
#: not match ``52``-plus-counter-suffix in ``README.bn.md``. Lookarounds on
#: digits only are what make this work in every script.
_INT_RE = re.compile(r"(?<![0-9])\d+(?![0-9])")

#: An l10n binding comment. Stripped before scanning a section for integers:
#: its ``sha256:`` hash is hex, so a hash containing a run like ``54`` would
#: otherwise satisfy a claim the prose no longer states. The current
#: ``_find_translated_section`` already excludes the binding line; stripping it
#: here keeps this guard correct if that ever changes.
_BINDING_LINE_RE = re.compile(r"^.*<!--\s*l10n:.*$", re.MULTILINE)

#: "52" followed by the Bengali counter suffix U+099F U+09BF ("ti"), written as
#: escapes so this file stays ASCII. Proves the regex choice above rather than
#: asserting it.
_BENGALI_COUNTER_SAMPLE = "52\u099f\u09bf"


def _claimed(pattern: re.Pattern[str], page: Path = README) -> int:
    """Return the single integer *pattern* captures in *page*.

    Defaults to the English README, which is the source of every expected
    value: the claim patterns are English prose and match no translated page.
    """
    matches = pattern.findall(page.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected exactly one {page.name} match for {pattern.pattern!r}, got {matches}"
    return int(matches[0])


def _scannable(body: str) -> str:
    """Return *body* with l10n binding comments removed."""
    return _BINDING_LINE_RE.sub("", body)


def _claim_appears(body: str, value: int) -> bool:
    """True when *value* appears in *body* as a standalone integer."""
    return re.search(rf"(?<![0-9]){value}(?![0-9])", body) is not None


def _integers(body: str) -> list[int]:
    """Return every standalone integer in *body*, for failure messages."""
    return [int(match) for match in _INT_RE.findall(body)]


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


def test_readme_selectable_adapter_count_matches_the_registry() -> None:
    """The README's selectable-adapter count equals the registry's selectable set."""
    from bernstein.adapters.registry import selectable_adapter_names

    assert _claimed(_SELECTABLE_CLAIM_RE) == len(selectable_adapter_names())


def test_readme_intro_adapter_count_matches_the_registry() -> None:
    """The intro paragraph's "N more" count equals the selectable set minus the three named adapters (#6060, #6167).

    The intro and the at-a-glance bullet both restate the "supported agents"
    section's selectable count elsewhere on the page. Nothing checked either
    copy against the registry, so they could drift independently of the
    section-level guards above.

    "N more" is additive to the three adapters the sentence already names, so
    N has to exclude them: asserting N against the raw selectable count would
    pin a sentence that overstates the total by ``len(_NAMED_IN_INTRO)`` (#6167
    review) - correct today only because the overclaim happened to read the
    same as another number, not because the arithmetic was right.
    """
    from bernstein.adapters.registry import selectable_adapter_names

    assert _claimed(_INTRO_CLAIM_RE) == len(selectable_adapter_names()) - len(_NAMED_IN_INTRO)


def test_readme_at_a_glance_adapter_count_matches_the_registry() -> None:
    """The at-a-glance bullet's selectable-adapter count equals the registry's selectable set (#6060).

    ``generic`` is itself one of the selectable names, so the bullet has to
    say the count *includes* it rather than count it a second time with
    "plus" (#6167 review) - the number is the raw selectable count either
    way, but only the "including" wording is true of it.
    """
    from bernstein.adapters.registry import selectable_adapter_names

    assert _claimed(_AT_A_GLANCE_CLAIM_RE) == len(selectable_adapter_names())


def test_named_in_intro_are_all_selectable() -> None:
    """``_NAMED_IN_INTRO`` must name adapters the registry actually selects.

    The intro test's subtraction is only correct while every name it
    subtracts is itself counted in ``len(selectable_adapter_names())``; a
    rename or removal that desyncs the constant from the sentence would
    otherwise make the intro test's arithmetic wrong in a way nothing here
    would notice.
    """
    from bernstein.adapters.registry import selectable_adapter_names

    selectable = selectable_adapter_names()
    missing = [name for name in _NAMED_IN_INTRO if name not in selectable]
    assert not missing, f"{missing} named in the intro sentence but not in selectable_adapter_names(): {selectable}"


def test_docs_index_description_adapter_count_matches_the_registry() -> None:
    """The MkDocs Home page's SEO ``description`` restates the intro's "N more" claim (#6167 review).

    #6060 was about the README's count falling behind the registry; the same
    sentence shape ships in this page's front matter too, and nothing
    checked it. Same arithmetic as the README intro: the three named
    adapters are already selectable, so "N more" excludes them.
    """
    from bernstein.adapters.registry import selectable_adapter_names

    claimed = _claimed(_DOCS_INDEX_DESCRIPTION_CLAIM_RE, page=DOCS_INDEX)
    assert claimed == len(selectable_adapter_names()) - len(_NAMED_IN_INTRO)


def test_docs_index_feature_bullet_adapter_count_matches_the_registry() -> None:
    """The MkDocs Home page's "Any agent, any model" bullet states the selectable-adapter count (#6167 review)."""
    from bernstein.adapters.registry import selectable_adapter_names

    claimed = _claimed(_DOCS_INDEX_FEATURE_CLAIM_RE, page=DOCS_INDEX)
    assert claimed == len(selectable_adapter_names())


def test_install_matrix_is_a_subset_claim_not_a_full_one() -> None:
    """The matrix must stay smaller than or equal to the enumerated set.

    A matrix larger than the registry means the table lists agents that no
    adapter drives - the inverse of the drift this module guards against.
    """
    assert len(_install_matrix_rows()) <= len(_enumerate_rows())


# ---------------------------------------------------------------------------
# Translated pages
# ---------------------------------------------------------------------------


def test_translated_readmes_are_discovered() -> None:
    """There is at least one translated page to check.

    Without this, an empty glob would parametrise zero cases and the whole
    translated-page guard would report green while checking nothing - the
    silent drop-out this module exists to prevent.
    """
    assert TRANSLATED_READMES, f"no translated READMEs found under {I18N_DIR}"


@pytest.mark.parametrize("page", TRANSLATED_READMES, ids=lambda page: page.name)
def test_translated_readme_repeats_the_english_counts(page: Path) -> None:
    """Every translated page states the same counts the English page does.

    Coverage is strict: a page whose ``supported agents`` binding cannot be
    found fails here rather than skipping, so a restructured translation is
    reported instead of quietly dropping out of coverage.
    """
    section = _find_translated_section(page.read_text(encoding="utf-8"), EN_SECTION_HEADING)
    assert section is not None, (
        f"{page.name}: no '{EN_SECTION_HEADING}' l10n binding found. "
        f"Every page under {I18N_DIR.name}/ must carry one so its counts can be checked; "
        f"coverage here is deliberately strict rather than skipping the page."
    )

    body = _scannable(section.body)
    found = _integers(body)

    for label, pattern in _CLAIMS:
        expected = _claimed(pattern)
        assert _claim_appears(body, expected), (
            f"{page.name}: '{label}' count is stale - README.md claims {expected}, "
            f"but that number does not appear in the '{EN_SECTION_HEADING}' section. "
            f"Integers found there: {found}. "
            f"Update the translated sentence; do not run 'bernstein readme-l10n sync', "
            f"which rebinds structural hashes without translating and would hide this."
        )


def test_word_boundary_would_miss_a_bengali_counter_suffix() -> None:
    """The lookaround in ``_claim_appears`` is load-bearing, not decoration.

    Python's ``\\b`` is Unicode-aware and a Bengali letter is a word character,
    so there is no boundary between a digit and a Bengali counter suffix. This
    case fails under ``\\b`` and passes under the lookaround, which is what
    makes the choice of pattern a tested decision. ``README.bn.md`` exercises
    it on all three counts today.
    """
    assert re.search(r"\b52\b", _BENGALI_COUNTER_SAMPLE) is None, (
        r"\b52\b unexpectedly matched a Bengali counter suffix; "
        r"if Python's word-boundary semantics changed, re-check _claim_appears"
    )
    assert _claim_appears(_BENGALI_COUNTER_SAMPLE, 52)


def test_binding_hash_digits_cannot_satisfy_a_claim() -> None:
    """A hex hash containing a claim's digits must not count as the claim.

    ``sha256:`` hashes are hex, so a binding line can contain a run like
    ``54``. Stripping binding lines before scanning is what stops a stale
    translation from passing on its own hash.
    """
    body = '<!-- l10n: en="supported agents" hash="sha256:54ab30cd52ef" -->\n\nno counts here.\n'

    assert _integers(_scannable(body)) == []
    for value in (30, 52, 54):
        assert not _claim_appears(_scannable(body), value)
