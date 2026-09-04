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
