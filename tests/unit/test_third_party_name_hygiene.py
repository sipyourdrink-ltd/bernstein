"""Guard: user-facing text describes this project, it does not compare it.

``bernstein workflow`` once described its manifests by naming an
unrelated third-party tool and calling them "<that tool>-style". The
command does not need another product's name to say what it does, and
help strings do not stay in the terminal: the docs site scrapes
``bernstein --help`` into its generated CLI reference, so a name in a
help string becomes a name on a published page. #2694 replaced that
string with "declarative YAML workflow manifests"; nothing stopped the
next one.

The defect is narrower than "a proper noun appeared". Help text names
products all the time for good reasons - the adapter catalogue is a list
of other people's tools, and interop claims have to name the format they
interoperate with. What went wrong is the *construction*: a proper noun
used attributively to characterise our own feature by resemblance
("X-style", "X-inspired", "inspired by X"). That says nothing a reader
can act on, and it ships a name the project never decided to ship.

So this guard matches comparative constructions, not names, and resolves
whatever noun they capture against an allow-list that is mostly derived
rather than curated:

* the shipped adapter registry, plus each adapter's provider aliases -
  naming a tool we ship an adapter for is the catalogue doing its job,
  and a newly registered adapter becomes nameable with no edit here;
* the scanner registry, on the same reasoning;
* :data:`_INTEROP_VOCABULARY`, the standards, formats, protocols,
  algorithms and libraries this project is built on or interoperates
  with - naming those is how you describe an implementation;
* :data:`_ORDINARY_WORDS`, capitalised English and Bernstein's own
  subsystem names, which the pattern cannot tell from a product name.

A name that is in none of those is a product this project has not
decided to ship the name of. The fix is nearly always to say what the
thing does instead; if the name genuinely belongs, it goes in the
vocabulary above as a deliberate, reviewed act.

Companion to :mod:`tests.unit.test_docs_url_hygiene`, which pins the same
"what leaves the wheel" boundary for links.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from bernstein.adapters import iter_scanner_specs
from bernstein.adapters.registry import iter_adapter_specs
from bernstein.cli.main import cli

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "docs"

# Modifiers that make the preceding noun a comparison. Deliberately excludes
# ``-based`` and ``-compatible``: "File-based", "Role-based" and
# "OpenAI-compatible" state how something is implemented or what it
# interoperates with, which is description, not resemblance.
_COMPARATIVE_MODIFIER = r"(?:style|styled|inspired|like|esque|flavoured|flavored|clone)"

_HYPHENATED = re.compile(rf"\b([A-Z][A-Za-z0-9.]{{1,24}})-{_COMPARATIVE_MODIFIER}\b")

_PHRASAL = re.compile(
    r"\b(?:inspired\s+by|similar\s+to|(?:modelled|modeled)\s+(?:on|after)|a\s+port\s+of"
    r"|an?\s+alternative\s+to|compared\s+(?:to|with)|unlike|in\s+the\s+style\s+of"
    r"|equivalent\s+of|answer\s+to|drop-in\s+replacement\s+for)\s+"
    r"([A-Z][A-Za-z0-9.]{1,24})"
)

#: Standards, formats, protocols, algorithms and libraries this project is
#: built on or interoperates with. Comparing a surface to one of these
#: describes the surface ("JWT-style detached signature" tells a reader the
#: wire shape); it does not position the project against a product.
_INTEROP_VOCABULARY: frozenset[str] = frozenset(
    {
        "ADR",
        "AP2",
        "Click",
        "DNS",
        "JSONPath",
        "JWT",
        "Jinja",
        "Kademlia",
        "MCP",
        "MIME",
        "SLSA",
        "SSRF",
        "Sigstore",
        "TF",
        "US",
        "Unix",
    }
)

#: Capitalised words the pattern cannot distinguish from a product name:
#: ordinary English at the head of a compound, and this project's own name
#: and subsystem names.
_ORDINARY_WORDS: frozenset[str] = frozenset(
    {
        "Bernstein",
        "Bulletin",
        "Lineage",
        "Path",
        "Prefix",
        "Response",
    }
)


def _shipped_names() -> frozenset[str]:
    """Return the names the shipped registries make legitimate to mention.

    Derived, not curated: registering an adapter or a scanner is what makes
    its name mentionable, so the allow-list cannot drift out of step with
    what the wheel actually ships.
    """
    names: set[str] = set()
    for name, adapter in iter_adapter_specs():
        names.add(name)
        aliases = getattr(adapter, "provides", None)
        if isinstance(aliases, (list, tuple, set, frozenset)):
            names.update(str(alias) for alias in aliases)
    names.update(name for name, _ in iter_scanner_specs())
    # Registry names are lower-case/underscored ("openai_agents", "q_dev");
    # prose writes them "OpenAI"/"Codex". Compare on a flattened form.
    return frozenset(_normalise(name) for name in names)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]", "", name).casefold()


def _allowed() -> frozenset[str]:
    vocabulary = {_normalise(w) for w in _INTEROP_VOCABULARY | _ORDINARY_WORDS}
    return _shipped_names() | frozenset(vocabulary)


def _comparisons(text: str) -> list[tuple[str, str]]:
    """Return ``(captured-noun, matched-phrase)`` for every comparison in *text*."""
    found = [(m.group(1), m.group(0)) for m in _HYPHENATED.finditer(text)]
    found += [(m.group(1), m.group(0)) for m in _PHRASAL.finditer(text)]
    return found


def _help_texts() -> list[tuple[str, str]]:
    """Return ``(command-path, text)`` for every string ``--help`` can print.

    Walks the live Click tree rather than scanning source, because the live
    tree is what the docs scraper reads: a group's help, a command's help
    and short help, and every option's help string.
    """
    collected: list[tuple[str, str]] = []

    def walk(command: click.Command, prefix: tuple[str, ...]) -> None:
        path = " ".join(("bernstein", *prefix))
        for attribute in ("help", "short_help"):
            value = getattr(command, attribute, None)
            if isinstance(value, str) and value.strip():
                collected.append((path, value))
        for parameter in command.params:
            help_text = getattr(parameter, "help", None)
            if isinstance(help_text, str) and help_text.strip():
                collected.append((f"{path} [{parameter.name}]", help_text))
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                walk(sub, (*prefix, name))

    walk(cli, ())
    return collected


def _report(sites: list[str], surface: str) -> str:
    return (
        f"{surface} compares this project to a product that is not a shipped "
        f"adapter, scanner, or a standard in _INTEROP_VOCABULARY:\n"
        + "\n".join(sites)
        + "\n\nSay what the thing does instead of what it resembles: "
        '"<product>-style YAML workflow manifests" -> "declarative YAML '
        'workflow manifests". If the name genuinely belongs, add it to '
        "_INTEROP_VOCABULARY in this file with a reason."
    )


def test_cli_help_names_no_unshipped_product() -> None:
    allowed = _allowed()
    sites = [
        f"  {path}: {phrase!r}"
        for path, text in _help_texts()
        for noun, phrase in _comparisons(text)
        if _normalise(noun) not in allowed
    ]
    assert sites == [], _report(sites, "CLI help text")


def test_docs_pages_name_no_unshipped_product() -> None:
    allowed = _allowed()
    sites: list[str] = []
    for page in sorted(_DOCS.rglob("*.md")):
        try:
            text = page.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            sites += [
                f"  {page.relative_to(_REPO_ROOT)}:{lineno}: {phrase!r}"
                for noun, phrase in _comparisons(line)
                if _normalise(noun) not in allowed
            ]
    assert sites == [], _report(sites, "docs/ page")


def test_guard_catches_the_shape_it_was_written_for() -> None:
    """The #2694 defect shape must fail, and its replacement must pass.

    Uses a synthetic name rather than the one #2694 removed: the guard is
    about the construction, not about any particular product, and a
    fixture that carries a real name would reintroduce it here. Without
    this test a pattern that silently stopped matching would leave both
    scans green and the guard would report nothing forever.
    """
    allowed = _allowed()

    offending = _comparisons("Acmeflow-style YAML workflow manifests")
    assert offending, "the hyphenated comparison pattern stopped matching the #2694 shape"
    assert all(_normalise(noun) not in allowed for noun, _ in offending)

    phrasal = _comparisons("A workflow runner inspired by Acmeflow.")
    assert phrasal, "the phrasal comparison pattern stopped matching"
    assert all(_normalise(noun) not in allowed for noun, _ in phrasal)

    # The replacement #2694 shipped is what a clean help string looks like.
    assert _comparisons("Declarative YAML workflow manifests") == []

    # Describing implementation or interop is not a comparison.
    assert _comparisons("File-based store with an OpenAI-compatible endpoint") == []

    # A shipped adapter stays mentionable, so the allow-list is doing work.
    shipped = _comparisons("Codex-style sandbox semantics")
    assert shipped and all(_normalise(noun) in allowed for noun, _ in shipped)
