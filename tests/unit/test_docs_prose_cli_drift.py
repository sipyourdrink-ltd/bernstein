"""Narrative prose CLI drift gate (#3620).

Nothing checks narrative prose against the CLI. This gate extracts
``bernstein`` invocations from the pages a new reader lands on first and
checks them against the registered Click surface, so a page naming a flag
the CLI rejects, or a command that no longer resolves, fails a test
instead of failing a reader.

Design decisions (the extractor is the whole design problem; see #3620):

* Only ``bernstein``-prefixed invocations are claims. npm/git/pip/uv
  snippets and bare mentions of the product name are not invocations and
  are never extracted - the prefix is the first explicit filter.
* Fenced blocks: an executable fence language (``bash``, ``sh``, ``shell``,
  ``zsh``, ``console``) marks input commands and is extracted. A fence with
  no language, or an output language (``text``, ``output``, ``plaintext``,
  ``log``, ``txt``), marks an output sample and the whole block is
  excluded. This is the documented convention this repo already follows -
  every output sample in the corpus uses a language-less fence - and it is
  the explicit mechanism that stops output samples from producing false
  failures. Blocks in any other language (``yaml``, ``py``, ...) are not
  CLI claims and are ignored.
* Inline spans: `` `bernstein ...` `` is an invocation; a bare `` `--flag` ``
  span is a flag reference; any other span is not extracted. Headings are
  page structure, not command regions - an inline span in a heading names
  an event ("bernstein init fails") rather than declaring an invocation, so
  headings are never extracted. Invocation claims live in body prose and
  executable fenced blocks.
* Parsing: after ``bernstein``, flag tokens (``--x`` / ``-x``) and
  placeholders (``<...>``, ``$VAR``, ``...``) never form part of the
  command path. The path is the longest registered prefix of the remaining
  tokens. A trailing argument that looks like a path/value (contains ``/``
  or ``.``) is a parameter and is ignored; a trailing bare word that is
  not a registered subcommand fails as a concept that no longer resolves.
* The extractor reports what it checked AND what it declined to parse.
  Invocations that cannot be parsed at all (a bare ``bernstein``, or
  ``bernstein $VAR``, or ``bernstein ...``) land in the skipped counter.
  A fixture asserts the skipped count is non-zero for an unparseable page,
  so an extractor that quietly stops parsing something cannot slip through
  as green.
* Flags are checked against the union of every option the CLI registers,
  plus the Click builtins ``--help`` / ``-h`` / ``--version``. A flag in
  the union passes even if it belongs to a different command (no false
  positives); a flag in no command at all fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import click

REPO_ROOT = Path(__file__).resolve().parents[2]

CORPUS_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "docs" / "getting-started",
    REPO_ROOT / "docs" / "guides",
)

BUILTIN_FLAGS: frozenset[str] = frozenset({"--help", "-h", "--version"})

# Fences that mark input commands vs output samples (see module docstring).
_EXEC_FENCES: frozenset[str] = frozenset({"bash", "sh", "shell", "zsh", "console"})
_OUTPUT_FENCES: frozenset[str] = frozenset({"text", "output", "plaintext", "log", "txt"})

_FENCE_RE = re.compile(r"^[ \t]*```([A-Za-z0-9_+.-]*)[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_FLAG_TOKEN_RE = re.compile(r"^--[a-zA-Z][a-zA-Z0-9-]*$|^-[a-zA-Z]$")
_PLACEHOLDER_RE = re.compile(r"^<[^>]+>$")
_MAX_PATH_DEPTH = 3


@dataclass(frozen=True)
class CliSurface:
    """Registered command paths, accepted flags, and flags that take a value."""

    paths: frozenset[str]
    all_flags: frozenset[str]
    value_flags: frozenset[str]

    @classmethod
    def from_click(cls, cli: click.Group) -> CliSurface:
        paths: set[str] = set()
        flags: set[str] = set()
        value_flags: set[str] = set()

        def walk(group: click.Group, prefix: tuple[str, ...]) -> None:
            for name, cmd in group.commands.items():
                path = (*prefix, name)
                paths.add(" ".join(path))
                for param in getattr(cmd, "params", []):
                    if isinstance(param, click.Option):
                        flags.update(param.opts)
                        flags.update(param.secondary_opts)
                        if not param.is_flag:
                            value_flags.update(param.opts)
                            value_flags.update(param.secondary_opts)
                if isinstance(cmd, click.Group):
                    walk(cmd, path)

        walk(cli, ())
        flags |= BUILTIN_FLAGS
        return cls(
            paths=frozenset(paths),
            all_flags=frozenset(flags),
            value_flags=frozenset(value_flags),
        )


@dataclass
class Invocation:
    """One extracted invocation: tokens after ``bernstein``, flags included."""

    line: int
    tokens: tuple[str, ...]

    @property
    def flags(self) -> tuple[str, ...]:
        return tuple(t for t in self.tokens if _FLAG_TOKEN_RE.match(t))

    @property
    def non_flag_tokens(self) -> tuple[str, ...]:
        return tuple(t for t in self.tokens if not _FLAG_TOKEN_RE.match(t))


@dataclass
class Extraction:
    """Checked invocations and declined-to-parse rows (skipped)."""

    checked: list[Invocation] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)


def extract(markdown: str) -> Extraction:
    """Extract ``bernstein`` invocations from narrative markdown.

    Executable fenced blocks contribute whole lines; prose contributes
    inline code spans. Output-sample blocks (language-less or output
    languages) are excluded wholesale - by convention they are not
    invocations, so they are neither checked nor skipped.
    """
    result = Extraction()
    in_fence = False
    fence_lang = ""
    for lineno, raw in enumerate(markdown.splitlines(), 1):
        fence = _FENCE_RE.match(raw.strip())
        if fence:
            if in_fence:
                in_fence = False
                fence_lang = ""
            else:
                in_fence = True
                fence_lang = fence.group(1).strip().lower()
            continue
        if not in_fence:
            # Headings are page structure, not command regions: an inline
            # span in a heading names a page/event ("bernstein init fails")
            # rather than declaring an invocation. Claims live in body prose
            # and executable fenced blocks.
            if not raw.lstrip().startswith("#"):
                _extract_inline_spans(raw, lineno, result)
        elif fence_lang in _EXEC_FENCES:
            _extract_line(raw, lineno, result)
        # language-less / output / other-language fences: excluded
    return result


def _extract_inline_spans(line: str, lineno: int, result: Extraction) -> None:
    for span in _INLINE_CODE_RE.findall(line):
        stripped = span.strip()
        if stripped.startswith("bernstein"):
            _extract_line(stripped, lineno, result)
        elif _FLAG_TOKEN_RE.match(stripped):
            result.checked.append(Invocation(line=lineno, tokens=(stripped,)))


def _extract_line(line: str, lineno: int, result: Extraction) -> None:
    body = _strip_inline_comment(line).strip()
    if not body.startswith("bernstein"):
        return
    tokens = _tokenize(body)
    if not tokens or tokens[0] != "bernstein":
        return
    rest = tokens[1:]
    if not rest:
        result.skipped.append((lineno, "bare `bernstein` with no command"))
        return
    meaningful = [t for t in rest if not _is_placeholder(t)]
    if not meaningful:
        result.skipped.append((lineno, f"only placeholders: {' '.join(rest)}"))
        return
    result.checked.append(Invocation(line=lineno, tokens=tuple(rest)))


def _strip_inline_comment(line: str) -> str:
    """Remove a trailing ``# comment`` outside quotes (bash semantics)."""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _tokenize(line: str) -> list[str]:
    """Quote-aware whitespace tokenizer (single/double quotes)."""
    tokens: list[str] = []
    cur: list[str] = []
    in_single = in_double = False
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
            cur.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            cur.append(ch)
        elif ch.isspace() and not in_single and not in_double:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def _is_placeholder(tok: str) -> bool:
    return _PLACEHOLDER_RE.match(tok) is not None or tok.startswith("$") or tok in {"...", "…"} or tok.startswith("...")


def _looks_like_value(tok: str) -> bool:
    """A trailing argument that is a parameter, not a command name."""
    return "/" in tok or "." in tok or tok.startswith(("-", "<", '"', "'"))


def check(extraction: Extraction, surface: CliSurface, page: str) -> list[str]:
    """Check extracted invocations against the surface; return failure lines."""
    failures: list[str] = []
    for inv in extraction.checked:
        path_tokens: list[str] = []
        flags: list[str] = []
        i = 0
        tokens = inv.tokens
        while i < len(tokens):
            tok = tokens[i]
            if _FLAG_TOKEN_RE.match(tok):
                flags.append(tok)
                # Absorb the value of a flag that takes one: `--timeout 120`
                # and `-g "goal"` both have their value consumed here so the
                # value can never be mistaken for a command name.
                if tok in surface.value_flags and i + 1 < len(tokens):
                    nxt = tokens[i + 1]
                    if not _FLAG_TOKEN_RE.match(nxt) and not _is_placeholder(nxt):
                        i += 1  # skip the value token
            elif _is_placeholder(tok):
                pass
            elif _looks_like_value(tok):
                pass  # positional value / path argument, not a command
            else:
                path_tokens.append(tok)
            i += 1

        path = _resolve_path(tuple(path_tokens), surface)
        if path is None:
            if path_tokens:
                failures.append(f"{page}:{inv.line}: command 'bernstein {path_tokens[0]}' does not exist")
        else:
            _check_extra_subcommand(path, tuple(path_tokens), inv, surface, page, failures)
        for fl in flags:
            if fl not in surface.all_flags:
                failures.append(f"{page}:{inv.line}: flag {fl!r} is not accepted by the CLI")
    return failures


def _resolve_path(tokens: tuple[str, ...], surface: CliSurface) -> str | None:
    for n in range(min(_MAX_PATH_DEPTH, len(tokens)), 0, -1):
        cand = " ".join(tokens[:n])
        if cand in surface.paths:
            return cand
    return None


def _check_extra_subcommand(
    path: str,
    non_flag: tuple[str, ...],
    inv: Invocation,
    surface: CliSurface,
    page: str,
    failures: list[str],
) -> None:
    """A bare trailing word that is not a registered subcommand is a stale concept."""
    depth = len(path.split())
    rest = non_flag[depth:]
    if not rest:
        return
    head = rest[0]
    if _looks_like_value(head):
        return  # parameter, not a command name
    extended = " ".join(non_flag[: depth + 1])
    if extended not in surface.paths:
        failures.append(f"{page}:{inv.line}: command 'bernstein {extended}' does not exist")


def _surface() -> CliSurface:
    from bernstein.cli.main import cli  # local import keeps module import cheap

    return CliSurface.from_click(cli)


# ---------------------------------------------------------------------------
# Fixture tests: parsing rules are exercised against fixture markdown so the
# extractor is testable without the corpus.
# ---------------------------------------------------------------------------


def test_rejected_flag_fails_with_page_line_and_flag() -> None:
    page = '```bash\nbernstein run --goal-not-a-real-flag "do things"\n```\n'
    failures = check(extract(page), _surface(), "fixture.md")
    assert any("fixture.md:2" in f and "--goal-not-a-real-flag" in f for f in failures), failures


def test_stale_concept_fails_with_page_line_and_command() -> None:
    # `agents show` is not a registered subcommand - a reader following this
    # page would hit a CLI error. This is the #3514 disease shape.
    page = "```bash\nbernstein agents show --json\n```\n"
    failures = check(extract(page), _surface(), "fixture.md")
    assert any("fixture.md:2" in f and "bernstein agents show" in f for f in failures), failures


def test_unparseable_constructs_land_in_skipped() -> None:
    page = "```bash\nbernstein ...\nbernstein $COMMAND\nbernstein\n```\n"
    ext = extract(page)
    assert len(ext.skipped) == 3, ext.skipped
    assert ext.checked == []
    # Lines 2..4 of the fixture (line 1 is the fence opener).
    assert {line for line, _ in ext.skipped} == {2, 3, 4}


def test_output_samples_are_not_checked() -> None:
    page = (
        "```bash\n"
        "bernstein --version\n"
        "```\n"
        "\n"
        "```\n"
        "bernstein 3.5.0\n"
        "```\n"
        "\n"
        "```text\n"
        '✓ Ready - run `bernstein -g "your goal"` to start\n'
        "```\n"
    )
    ext = extract(page)
    # Only the executable block contributes; both output blocks are excluded.
    assert len(ext.checked) == 1, [(i.line, i.tokens) for i in ext.checked]
    assert ext.checked[0].tokens == ("--version",)
    assert ext.skipped == []


def test_inline_spans_are_extracted() -> None:
    page = (
        "Verify with `bernstein --version`.\n"
        "The `--remote` flag skips local probes.\n"
        'PATH note: `export PATH="$HOME/.local/bin:$PATH"` is not a claim.\n'
    )
    ext = extract(page)
    assert len(ext.checked) == 2, [(i.line, i.tokens) for i in ext.checked]
    assert ext.checked[0].tokens == ("--version",)
    assert ext.checked[1].tokens == ("--remote",)


def test_trailing_parameter_is_not_a_command() -> None:
    page = "```bash\nbernstein run plans/hello.yaml\n```\n"
    failures = check(extract(page), _surface(), "fixture.md")
    assert failures == []


def test_flag_values_are_not_mistaken_for_commands() -> None:
    page = (
        "```bash\n"
        "bernstein demo --flask-todo --timeout 120\n"
        "bernstein status --mode expert\n"
        "bernstein stop --timeout 3\n"
        "```\n"
    )
    failures = check(extract(page), _surface(), "fixture.md")
    assert failures == []


def test_reverting_slice1_fix_fails() -> None:
    # Slice 1 (#3619/#3623) replaced `bernstein agents` used to *inspect*
    # adapters with `bernstein doctor`. The pre-fix page shape that the gate
    # must catch: an invocation naming a surface the CLI no longer exposes,
    # or a flag that exists nowhere in the CLI.
    pre_fix = "```bash\nbernstein adapters verifyx --agent-status\n```\n"
    failures = check(extract(pre_fix), _surface(), "fixture.md")
    assert any("bernstein adapters verifyx" in f for f in failures), failures
    assert any("--agent-status" in f for f in failures), failures


# ---------------------------------------------------------------------------
# Corpus test: the gate over the pages a new reader lands on first.
# ---------------------------------------------------------------------------


def test_narrative_docs_have_no_cli_drift() -> None:
    surface = _surface()
    failures: list[str] = []
    stats: list[str] = []
    total_checked = 0
    total_skipped = 0
    for corpus_dir in CORPUS_DIRS:
        for page in sorted(corpus_dir.glob("*.md")):
            ext = extract(page.read_text(encoding="utf-8"))
            total_checked += len(ext.checked)
            total_skipped += len(ext.skipped)
            stats.append(f"{page.name}: checked={len(ext.checked)} skipped={len(ext.skipped)}")
            failures.extend(check(ext, surface, page.name))
    assert total_checked > 0, "extractor found no invocations - the gate would be silent"
    assert not failures, (
        "Narrative docs drift from the registered CLI surface:\n"
        + "\n".join(failures)
        + "\n\nPer-page extraction stats:\n"
        + "\n".join(stats)
        + f"\nTotal checked={total_checked} skipped={total_skipped}"
    )
