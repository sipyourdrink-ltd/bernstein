"""Docs guard: a README capability claim must match the symbol's use sites (#3127).

The defect this catches
-----------------------
A statement that is true of the schema and false of the behaviour is the hard
kind to catch by review, because the field really does exist and really does
validate. ``agent_kind`` shipped as a team-manifest key that parses, validates,
defaults and re-serialises, and the README said "a role declares its modality
via ``agent_kind``". Nothing on the execution path ever read it, so a role could
declare a modality and the run executed identically either way. The same shape
recurred for ``OutputMode.ARTIFACT`` (no adapter declares it) and
``artifact_spec`` (no seed, plan, backlog or CLI surface constructs one).

The rule
--------
For every capability watched below:

* count the **use sites** of its symbol - the files that mention it outside the
  file that defines it, or, for a config key, outside the operator-facing input
  surfaces it would have to reach;
* when there are **no** use sites the capability is *inert*, and the README must
  carry the scope marker that says so and must not carry the phrasing that
  asserts runtime behaviour;
* when a use site **appears** the capability became reachable, and the test
  fails the other way: the scope marker is now stale and must be removed
  together with the ``<!-- scope:... -->`` block it sits in.

Both directions matter. The first stops the claim from drifting ahead of the
code; the second stops the correction from outliving the gap it describes, so
the scoped paragraph is removed by whoever lands reachability rather than
lingering as a second inaccuracy pointing the other way.

This is a grep, not a call-graph analysis. It reads the symbol as text, so an
alias or a ``getattr`` lookup would slip past it. That is an accepted limit: the
class of defect it exists for is a key nothing reads at all, which greps
cleanly. Comments and docstrings are stripped before the grep, so *describing*
an inert key does not make it count as read.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "bernstein"


@dataclass(frozen=True)
class WatchedCapability:
    """A README capability claim tied to the symbol that would implement it."""

    #: Human name used in failure messages.
    name: str
    #: The token grepped for across ``src/bernstein/``.
    symbol: str
    #: Repo-relative files whose mentions do NOT count as a use site: the
    #: definition itself. Empty when ``reach_paths`` is set instead.
    definition_paths: tuple[str, ...]
    #: When set, ONLY these repo-relative paths (file or directory prefix)
    #: count as use sites. Used for a config key that is live inside the core
    #: but unreachable from the operator-facing input surfaces.
    reach_paths: tuple[str, ...] | None
    #: A repo-relative file that must contain ``symbol``, so a rename cannot
    #: quietly turn this guard into a check of nothing.
    anchor_path: str
    #: Substring the README must carry while the capability is inert.
    scope_marker: str
    #: Phrasings that assert the capability works at runtime. Banned while it
    #: does not.
    banned_phrases: tuple[str, ...]
    #: Issue that makes the capability reachable.
    tracking_issue: str


WATCHED: tuple[WatchedCapability, ...] = (
    WatchedCapability(
        name="role modality declaration (agent_kind)",
        symbol="agent_kind",
        definition_paths=("src/bernstein/core/teams/manifest.py",),
        reach_paths=None,
        anchor_path="src/bernstein/core/teams/manifest.py",
        scope_marker=("`agent_kind` is accepted and validated by the team manifest but no scheduler code reads it yet"),
        banned_phrases=(
            "A role declares its modality via `agent_kind`",
            "declares its modality via `agent_kind`",
        ),
        tracking_issue="#3110",
    ),
    WatchedCapability(
        name="artifact output mode (OutputMode.ARTIFACT)",
        symbol="OutputMode.ARTIFACT",
        definition_paths=("src/bernstein/adapters/_contract.py",),
        reach_paths=None,
        anchor_path="src/bernstein/adapters/_contract.py",
        scope_marker="Every bundled adapter declares `git-diff` output",
        banned_phrases=("the same control plane runs research, browser/computer-use, data, and ops agents",),
        tracking_issue="#2996",
    ),
    WatchedCapability(
        name="operator-declared artifact contract (artifact_spec)",
        symbol="artifact_spec",
        definition_paths=(),
        # The operator-facing input surfaces a seed / plan / backlog / CLI
        # declaration would have to pass through.
        reach_paths=(
            "src/bernstein/core/planning/plan_schema.py",
            "src/bernstein/core/planning/plan_loader.py",
            "src/bernstein/core/tasks/backlog_parser.py",
            "src/bernstein/cli/",
        ),
        anchor_path="src/bernstein/core/tasks/models.py",
        scope_marker=(
            "`artifact_spec` is not a field in a seed, plan, or backlog file, and there is no CLI option for it"
        ),
        banned_phrases=(),
        tracking_issue="#3110",
    ),
)


# ---------------------------------------------------------------------------
# Pure helpers - importable so the guard can be pointed at any README text
# ---------------------------------------------------------------------------


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _blank(lines: list[str], start: tuple[int, int], end: tuple[int, int]) -> None:
    """Blank the ``[start, end)`` region of ``lines`` (1-indexed rows)."""
    start_row, start_col = start
    end_row, end_col = end
    for row in range(start_row, end_row + 1):
        if row - 1 >= len(lines):
            break
        line = lines[row - 1]
        left = start_col if row == start_row else 0
        right = end_col if row == end_row else len(line)
        lines[row - 1] = line[:left] + " " * max(0, right - left) + line[right:]


def code_only(source: str) -> str:
    """Return ``source`` with comments and docstrings blanked out.

    A mention of a symbol inside a comment or a docstring is documentation, not
    a use site. Counting it would let a doc fix describing an inert key make
    that key look reachable, which is the exact confusion this guard exists to
    stop.
    """
    lines = source.splitlines(keepends=True)
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                _blank(lines, token.start, token.end)
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        return source
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return "".join(lines)
    doc_owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, doc_owners) or ast.get_docstring(node) is None:
            continue
        doc = node.body[0].value  # type: ignore[attr-defined]
        if doc.end_lineno is None or doc.end_col_offset is None:  # pragma: no cover
            continue
        _blank(lines, (doc.lineno, doc.col_offset), (doc.end_lineno, doc.end_col_offset))
    return "".join(lines)


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """True when ``path`` is one of ``prefixes`` or sits under one of them."""
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


@lru_cache(maxsize=1)
def _code_only_sources(src_root: Path = _SRC_ROOT) -> tuple[tuple[str, str], ...]:
    """``(repo-relative path, comment- and docstring-stripped source)`` for src/.

    Cached because every watched capability scans the same tree, and stripping
    it is the expensive half of this guard.
    """
    return tuple(
        (path.relative_to(_REPO_ROOT).as_posix(), code_only(path.read_text(encoding="utf-8")))
        for path in _python_files(src_root)
    )


def use_sites(capability: WatchedCapability, src_root: Path = _SRC_ROOT) -> list[str]:
    """Repo-relative files that count as a use site of ``capability.symbol``."""
    found: list[str] = []
    for rel, source in _code_only_sources(src_root):
        if capability.reach_paths is not None:
            if not _matches(rel, capability.reach_paths):
                continue
        elif _matches(rel, capability.definition_paths):
            continue
        if capability.symbol in source:
            found.append(rel)
    return found


def check_capability(capability: WatchedCapability, readme_text: str, sites: list[str]) -> list[str]:
    """Return the guard violations for one capability. Empty list means clean."""
    problems: list[str] = []

    if sites:
        # Reachability landed. The scope marker is now the inaccurate text.
        if capability.scope_marker in readme_text:
            problems.append(
                f"{capability.name}: `{capability.symbol}` now has use site(s) "
                f"{sites}, so the capability is reachable, but the README still "
                f"carries the scope text {capability.scope_marker!r}. Delete the "
                f"surrounding <!-- scope:... --> block and restore the capability "
                f"claim ({capability.tracking_issue})."
            )
        return problems

    # Inert: defined and validated, never read.
    for phrase in capability.banned_phrases:
        if phrase in readme_text:
            problems.append(
                f"{capability.name}: the README asserts {phrase!r}, but "
                f"`{capability.symbol}` has no use site outside its own "
                f"definition, so the claim is true of the schema and false of "
                f"the behaviour. Scope the sentence to what ships "
                f"({capability.tracking_issue})."
            )
    if capability.scope_marker not in readme_text:
        problems.append(
            f"{capability.name}: `{capability.symbol}` has no use site outside "
            f"its own definition, so the README must state the limit. Expected "
            f"to find {capability.scope_marker!r} in README.md "
            f"({capability.tracking_issue})."
        )
    return problems


def _readme_text() -> str:
    return (_REPO_ROOT / "README.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", WATCHED, ids=lambda c: c.symbol)
def test_readme_claim_matches_symbol_use_sites(capability: WatchedCapability) -> None:
    """A capability the README names must be as reachable as the README implies."""
    problems = check_capability(capability, _readme_text(), use_sites(capability))
    if problems:
        pytest.fail("\n\n".join(problems))


@pytest.mark.parametrize("capability", WATCHED, ids=lambda c: c.symbol)
def test_watched_symbol_still_exists(capability: WatchedCapability) -> None:
    """The watched symbol must still be defined, or this guard checks nothing.

    A renamed or deleted symbol makes every ``in`` check above trivially false,
    which would report "inert" forever. Anchoring on the defining file turns
    that into a loud failure instead.
    """
    anchor = _REPO_ROOT / capability.anchor_path
    if not anchor.is_file():
        pytest.fail(
            f"{capability.name}: anchor file {capability.anchor_path} no longer "
            f"exists. Update WATCHED in {Path(__file__).name} or drop the entry."
        )
    # ``OutputMode.ARTIFACT`` is written as a bare member inside its own enum
    # body, so anchor on the member name rather than the dotted reference.
    token = capability.symbol.rsplit(".", 1)[-1]
    if token not in code_only(anchor.read_text(encoding="utf-8")):
        pytest.fail(
            f"{capability.name}: {capability.anchor_path} no longer mentions "
            f"{token!r}. The symbol was renamed or removed, so this guard is "
            f"checking nothing. Update WATCHED in {Path(__file__).name}."
        )


def test_code_only_strips_prose_but_keeps_code() -> None:
    """A symbol named only in a comment or docstring is not a use site."""
    source = (
        '"""Module doc mentioning widget_key."""\n'
        "\n"
        "# comment mentioning widget_key\n"
        "def f(entry):\n"
        '    """Function doc mentioning widget_key."""\n'
        "    return entry\n"
    )
    assert "widget_key" not in code_only(source)

    live = 'def g(entry):\n    return entry.get("widget_key")\n'
    assert "widget_key" in code_only(live)

    trailing = "x = 1  # widget_key\ny = 2\n"
    stripped = code_only(trailing)
    assert "widget_key" not in stripped
    assert "x = 1" in stripped and "y = 2" in stripped


def test_scope_blocks_are_balanced() -> None:
    """Every ``<!-- scope:X start ... -->`` marker has a matching ``end``.

    The scoped paragraphs are written to be deleted whole. An unbalanced pair
    means half a correction was removed and the other half is still rendering.
    """
    docs = [
        _REPO_ROOT / "README.md",
        _REPO_ROOT / "docs" / "operations" / "activity-boundary.md",
        _REPO_ROOT / "docs" / "operations" / "artifacts.md",
    ]
    problems: list[str] = []
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for marker in ("activity-boundary-reachability", "artifact-spec-reachability"):
            starts = text.count(f"<!-- scope:{marker} start")
            ends = text.count(f"<!-- scope:{marker} end -->")
            if starts != ends:
                problems.append(
                    f"{doc.relative_to(_REPO_ROOT)}: scope:{marker} has "
                    f"{starts} start marker(s) and {ends} end marker(s)."
                )
    if problems:
        pytest.fail("\n".join(problems))
