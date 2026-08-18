"""``docs/reference/receipt.md`` must not describe a CLI surface that moved (#4063).

Every subcommand and flag the page names for ``bernstein receipt`` is checked
against the actual Click parser (``receipt_group`` in
``bernstein.cli.commands.receipt_cmd``), so a renamed or removed flag fails
here instead of only misleading a reader. The model is the same idea
``test_docs_prose_cli_drift.py`` applies to the getting-started/guides
corpus, scoped down to this one reference page rather than duplicating that
extractor's general Markdown-corpus machinery.

Two directions, both load-bearing:

* **Forward.** Every flag the *parser* actually registers for ``verify`` and
  ``create`` is mentioned somewhere on the page (as `` `--flag` `` inline
  code), so an added flag with no doc coverage fails here rather than
  shipping silently undocumented.
* **Reverse.** Every `` `--...` `` inline-code span on the page that looks
  like a flag is one the parser actually registers for *some* receipt
  subcommand, so a renamed or removed flag is caught the moment the page
  still claims it.

``--help`` is a Click builtin every subcommand carries implicitly and is
exempted from both directions, matching how ``BUILTIN_FLAGS`` is treated in
the general prose-drift gate.
"""

from __future__ import annotations

import re
from pathlib import Path

from bernstein.cli.commands.receipt_cmd import receipt_group

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAGE = _REPO_ROOT / "docs" / "reference" / "receipt.md"

_BUILTIN_FLAGS: frozenset[str] = frozenset({"--help"})

#: `` `--flag` ``, `` `-o` ``, or `` `--flag TYPE` `` (the table header rows
#: pair a flag with its placeholder type inside one span) -- the flag is the
#: token immediately after the opening backtick; anything after it up to the
#: closing backtick is metavar text, not part of the match.
_INLINE_FLAG_RE = re.compile(r"`(-{1,2}[a-zA-Z][a-zA-Z0-9-]*)(?:\s[^`]*)?`")


def _registered_flags() -> frozenset[str]:
    """Every ``--flag`` / ``-x`` opt string the receipt group's subcommands register."""
    flags: set[str] = set()
    for command in receipt_group.commands.values():
        for param in command.params:
            flags.update(opt for opt in getattr(param, "opts", []) if opt.startswith("-"))
    return frozenset(flags)


def _flags_mentioned_on_page() -> frozenset[str]:
    text = _PAGE.read_text(encoding="utf-8")
    return frozenset(_INLINE_FLAG_RE.findall(text))


def test_page_exists() -> None:
    assert _PAGE.is_file(), f"{_PAGE} is missing"


def test_every_registered_flag_is_documented() -> None:
    """A flag the parser accepts and the page never mentions is undocumented, not a pass."""
    registered = _registered_flags() - _BUILTIN_FLAGS
    mentioned = _flags_mentioned_on_page()
    missing = sorted(registered - mentioned)
    assert not missing, f"receipt.md never mentions these registered flags: {missing}"


def test_page_names_no_flag_the_parser_does_not_accept() -> None:
    """A flag the page claims and the parser rejects is the exact drift this gate exists for."""
    registered = _registered_flags() | _BUILTIN_FLAGS
    mentioned = _flags_mentioned_on_page()
    stale = sorted(mentioned - registered)
    assert not stale, f"receipt.md names flags the parser does not register: {stale}"


def test_both_subcommands_are_named() -> None:
    """The group's own two subcommands, not a hardcoded guess at what it should contain."""
    text = _PAGE.read_text(encoding="utf-8")
    for name in receipt_group.commands:
        assert f"bernstein receipt {name}" in text, f"receipt.md never shows a `bernstein receipt {name}` invocation"
