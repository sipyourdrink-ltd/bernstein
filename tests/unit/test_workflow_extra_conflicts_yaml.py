"""``uv sync --all-extras`` must not request a declared-conflicting extra pair.

``[tool.uv].conflicts`` in ``pyproject.toml`` makes some extras mutually
exclusive. ``uv sync --all-extras`` asks for every extra at once, so any
workflow using it plainly fails at resolution time with
``Extras ... are incompatible with the declared conflicts`` and the job never
reaches its tests. This test ties the two together: adding a conflict pair
without excluding one side from an ``--all-extras`` step is a resolution
failure, and it should be caught here rather than in CI minutes.

The scan reads the workflow as YAML, looks only at ``run`` values, and lexes
each one with :class:`shlex.shlex` in punctuation mode so that shell operators
are recognised as tokens rather than matched as text. Splitting on ``&&`` with a
regex before tokenizing gets this wrong in both directions: an operator inside a
quoted argument splits a command that should stay whole, and the resulting
fragments then fail to lex.

A guard that cannot read a command must say so rather than skip it. The one case
where skipping is provably safe is a line that fails to lex and does not contain
the literal string ``--all-extras`` anywhere in its raw text -- quoting cannot
hide a substring from a substring search, so such a line cannot be a command
this guard is about. Every other unreadable line is a failure naming the
workflow.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: A backslash-continued newline: the two lines are one command.
_CONTINUATION = re.compile(r"\\\s*\n\s*")
#: Tokens ``shlex`` returns in punctuation mode that end one logical command.
_OPERATORS = frozenset({"&&", "||", "|", ";", ";;", "&", "(", ")"})
#: The flag whose presence this guard is about, as a literal substring.
_ALL_EXTRAS_LITERAL = "--all-extras"


class UnreadableCommand(Exception):
    """A ``run`` line mentioning ``--all-extras`` that could not be lexed."""


def _conflict_groups() -> list[set[str]]:
    """Return each declared conflict group as a set of extra names."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    raw = data.get("tool", {}).get("uv", {}).get("conflicts", [])
    groups: list[set[str]] = []
    for group in raw:
        extras = {entry["extra"] for entry in group if "extra" in entry}
        if len(extras) > 1:
            groups.append(extras)
    return groups


def _run_values(workflow: object) -> Iterator[str]:
    """Yield every ``run`` string reachable from a parsed workflow."""
    if isinstance(workflow, Mapping):
        for key, value in workflow.items():
            if key == "run" and isinstance(value, str):
                yield value
            else:
                yield from _run_values(value)
    elif isinstance(workflow, list):
        for item in workflow:
            yield from _run_values(item)


def _lex(line: str) -> Iterator[list[str]]:
    """Yield each logical command in one already-joined line."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    current: list[str] = []
    for token in lexer:
        if token in _OPERATORS:
            if current:
                yield current
                current = []
        else:
            current.append(token)
    if current:
        yield current


def _commands(run_value: str) -> Iterator[list[str]]:
    """Yield the token list of each logical command in one ``run`` value.

    Raises:
        UnreadableCommand: the line could not be lexed and mentions
            ``--all-extras``, so the guard cannot vouch for it.
    """
    joined = _CONTINUATION.sub(" ", run_value)
    for line in joined.splitlines():
        if not line.strip():
            continue
        try:
            # Materialised inside the try: the lexer raises while iterating.
            commands = list(_lex(line))
        except ValueError as exc:
            if _ALL_EXTRAS_LITERAL in line:
                raise UnreadableCommand(line.strip()) from exc
            continue
        yield from commands


def _syncs_all_extras(tokens: list[str]) -> bool:
    """Whether this command is a ``uv sync`` requesting every extra."""
    if _ALL_EXTRAS_LITERAL not in tokens:
        return False
    return any(tok == "uv" and tokens[i + 1 : i + 2] == ["sync"] for i, tok in enumerate(tokens))


def _excluded_extras(tokens: list[str]) -> set[str]:
    """Extras removed from the request by ``--no-extra``, in either spelling."""
    excluded: set[str] = set()
    for index, token in enumerate(tokens):
        if token == "--no-extra" and index + 1 < len(tokens):
            excluded.add(tokens[index + 1])
        elif token.startswith("--no-extra="):
            excluded.add(token.partition("=")[2])
    return excluded


def _all_extras_commands() -> tuple[list[tuple[Path, list[str]]], list[str]]:
    """Return ``(workflow, tokens)`` pairs that sync all extras, and unreadable lines."""
    found: list[tuple[Path, list[str]]] = []
    unreadable: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        for run_value in _run_values(loaded):
            try:
                commands = list(_commands(run_value))
            except UnreadableCommand as exc:
                unreadable.append(f"{path.name}: {exc}")
                continue
            found.extend((path, tokens) for tokens in commands if _syncs_all_extras(tokens))
    return found, unreadable


def test_conflict_groups_are_declared() -> None:
    """The guard below is only meaningful while a conflict is declared."""
    assert _conflict_groups(), "pyproject declares no [tool.uv] conflicts; drop this guard"


def test_conflicting_extras_are_declared_not_merely_unsatisfiable() -> None:
    """``modal`` and ``otel`` both pin a protobuf major via generated code; declare it.

    modal caps protobuf below 7.0; opentelemetry-exporter-otlp-proto-grpc pins
    protobuf==7.35.1 from 1.41.1 onward (#4088) -- the same clash shape as the
    checked-in gRPC gencode already declares against modal, just from a second
    source. Undeclared, this doesn't fail as a known-incompatible pair: it
    fails the *whole* lockfile as unsatisfiable, which is what aborted every
    ``uv`` dependency-update run rather than just the two extras that clash.
    Pinned as its own conflict group, distinct from the existing grpc x modal
    pair, so a future extra reintroducing the same protobuf clash fails here
    rather than three weeks later in a dependabot log nobody reads.
    """
    groups = _conflict_groups()
    assert {"grpc", "modal"} in groups
    assert {"otel", "modal"} in groups


def test_every_all_extras_command_is_readable() -> None:
    """A command the guard cannot lex is a gap in the guard, not a pass."""
    _, unreadable = _all_extras_commands()
    assert not unreadable, "workflow lines mention --all-extras but could not be lexed:\n" + "\n".join(unreadable)


def test_all_extras_syncs_exclude_one_side_of_every_conflict() -> None:
    """Each ``--all-extras`` step must exclude all but one extra per conflict group."""
    groups = _conflict_groups()
    commands, _ = _all_extras_commands()
    offenders: list[str] = []

    for path, tokens in commands:
        excluded = _excluded_extras(tokens)
        for group in groups:
            remaining = group - excluded
            if len(remaining) > 1:
                offenders.append(
                    f"{path.name}: {shlex.join(tokens)} -> requests conflicting extras {sorted(remaining)}"
                )

    assert not offenders, "uv sync --all-extras requests a declared-conflicting extra pair:\n" + "\n".join(offenders)


def test_all_extras_syncs_are_frozen() -> None:
    """A lane requesting every extra must resolve from the checked-in lock."""
    commands, _ = _all_extras_commands()
    assert commands, "no --all-extras sync found; this guard has nothing to check"
    unfrozen = [f"{path.name}: {shlex.join(tokens)}" for path, tokens in commands if "--frozen" not in tokens]
    assert not unfrozen, "uv sync --all-extras re-resolves instead of using uv.lock:\n" + "\n".join(unfrozen)


def test_operators_inside_quotes_do_not_split_a_command() -> None:
    """A quoted ``&&`` or ``|`` is an argument, not a command boundary."""
    quoted_amp = 'uv sync --frozen --all-extras --no-extra modal --index-url "https://mirror/?a=1&&b=2"'
    tokens = [t for t in _commands(quoted_amp) if _syncs_all_extras(t)]
    assert len(tokens) == 1, "a quoted && split the command"
    assert _excluded_extras(tokens[0]) == {"modal"}

    quoted_pipe = "uv sync --all-extras --no-extra modal --config-setting 'dir|name'"
    tokens = [t for t in _commands(quoted_pipe) if _syncs_all_extras(t)]
    assert len(tokens) == 1, "a quoted | split the command"
    assert _excluded_extras(tokens[0]) == {"modal"}


def test_unreadable_all_extras_line_is_raised_not_skipped() -> None:
    """An unbalanced quote around an --all-extras sync fails; unrelated lines do not."""
    with pytest.raises(UnreadableCommand):
        list(_commands('uv sync --all-extras --index-url "unterminated'))

    # No --all-extras in the raw text, so quoting cannot be hiding one.
    assert list(_commands('echo "unterminated')) == []


def test_scan_reads_continuations_quotes_and_comments() -> None:
    """The shapes a raw-text scan gets wrong."""
    continued = "uv sync --frozen \\\n  --all-extras --no-extra modal\n"
    tokens = [t for t in _commands(continued) if _syncs_all_extras(t)]
    assert len(tokens) == 1
    assert _excluded_extras(tokens[0]) == {"modal"}

    assert _excluded_extras(next(_commands('uv sync --all-extras --no-extra "modal"'))) == {"modal"}
    assert _excluded_extras(next(_commands("uv sync --all-extras --no-extra=modal"))) == {"modal"}

    assert not [t for t in _commands('echo "uv sync --all-extras"') if _syncs_all_extras(t)]
    assert not [t for t in _commands("# uv sync --all-extras") if _syncs_all_extras(t)]

    # Genuine operators still separate commands.
    both = "uv sync --all-extras --no-extra modal && uv run pytest -q"
    assert len([t for t in _commands(both) if _syncs_all_extras(t)]) == 1


def test_scan_ignores_yaml_outside_run_steps() -> None:
    """A ``--all-extras`` string somewhere other than a ``run`` is not a step."""
    workflow = {"jobs": {"a": {"steps": [{"name": "uv sync --all-extras", "uses": "actions/checkout@v4"}]}}}
    assert list(_run_values(workflow)) == []
