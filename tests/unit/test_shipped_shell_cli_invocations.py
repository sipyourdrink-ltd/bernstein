"""Every ``bernstein`` invocation in a shipped shell script must parse.

Companion to ``test_shipped_compose_cli_commands.py``, which guards the
subcommand names in shipped compose files. That guard stops at the first
token starting with ``-``, so an invocation naming a real subcommand with an
option that subcommand does not declare is invisible to it. Click rejects an
unknown option before running anything, so such a line is a hard exit 2 that
no amount of runtime robustness recovers from.

This is the shape that hit ``action/entrypoint.sh``: the GitHub Action's plan
mode ran ``bernstein run <plan> --budget <n> --headless``, but ``--headless``
is declared on the root ``bernstein`` group, not on the ``run`` subcommand.
Every plan-mode run of the published Action failed at option parsing with
"No such option: --headless" and never started.

Both the command path and every option are resolved against the real Click
tree in ``bernstein.cli.main``, so renaming or moving an option fails here
rather than in an operator's CI.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from click.core import Command, Group, Parameter

from bernstein.cli.main import cli

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that never hold a shipped operator-facing shell script.
_EXCLUDED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", ".tox", ".mypy_cache", "dist", "build"})

# Shell control operators. Everything from the first one of these onward
# belongs to a different command, so token scanning stops there.
_SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", ";;", "&", ">", ">>", "<", "(", ")", "{", "}", "#"})

# Tokens that may precede a command word in the shell forms this repo uses.
_COMMAND_PREFIXES = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "$(", "time"})


def _option_map(command: Command) -> dict[str, Parameter]:
    """Map every option string this command accepts to its parameter."""
    mapping: dict[str, Parameter] = {}
    for param in command.params:
        for opt in (*param.opts, *param.secondary_opts):
            if opt.startswith("-"):
                mapping[opt] = param
    return mapping


def _takes_a_value(param: Parameter) -> bool:
    """True when the next token is this option's value rather than a word.

    A flag consumes nothing, so the token after it is the next word. A
    value-taking option swallows the token after it, which must therefore not
    be mistaken for a subcommand name.
    """
    return not getattr(param, "is_flag", False) and getattr(param, "nargs", 1) != 0


def _bernstein_argv_lines(path: Path) -> list[tuple[int, list[str]]]:
    """Return ``(line_number, argv_after_the_binary)`` for each invocation.

    Only ``bernstein`` in *command* position counts. A mention inside an
    ``echo`` string or a comment is documentation, not something the shell
    will execute, and this repo has several of both.
    """
    found: list[tuple[int, list[str]]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            # `punctuation_chars` keeps `;`, `&&`, `||`, `|`, `<`, `>` as
            # separate tokens. Plain `shlex.split` would hand back
            # `--headless;` from `... --headless; then`, which reads as an
            # unknown option and would report a defect that is not there.
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            # Unbalanced quotes across a line continuation: not parseable in
            # isolation, and not a shape this repo's scripts use for the CLI.
            continue
        for index, token in enumerate(tokens):
            if token != "bernstein":
                continue
            if index > 0 and tokens[index - 1] not in _COMMAND_PREFIXES | _SHELL_OPERATORS:
                continue  # not in command position (e.g. an echoed word)
            argv: list[str] = []
            for candidate in tokens[index + 1 :]:
                if candidate in _SHELL_OPERATORS or candidate.startswith(("|", "&", ";", ">", "2>")):
                    break
                argv.append(candidate)
            found.append((lineno, argv))
            break
    return found


def _resolve(argv: list[str]) -> tuple[bool, str]:
    """Walk one invocation against the real Click tree.

    Returns ``(ok, detail)``. Options are checked against whichever command
    is current at that point, since an option declared on the root group is
    not accepted after a subcommand name and vice versa - which is exactly
    the defect this guard exists for.
    """
    current: Command = cli
    options = _option_map(current)
    path_parts: list[str] = []
    skip_next = False

    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if name == "--":
                break
            param = options.get(name)
            if param is None:
                where = f"bernstein {' '.join(path_parts)}".strip()
                return False, f"`{where}` does not accept the option `{name}`"
            if "=" not in token and _takes_a_value(param):
                skip_next = True
            continue
        if isinstance(current, Group) and token in current.commands:
            current = current.commands[token]
            options = _option_map(current)
            path_parts.append(token)
            continue
        # A bare token that is not a subcommand is a positional argument (a
        # plan file, a goal). Options after it still belong to `current`.
        if isinstance(current, Group) and not path_parts and not current.params:
            return False, f"`bernstein {token}` is not a registered CLI command"
    return True, f"`bernstein {' '.join(path_parts)}`".strip()


def _discover_shell_scripts() -> list[Path]:
    """Find every shell script in the repo that invokes the bernstein CLI.

    Discovered rather than hand-listed, for the same reason as the compose
    guard: a fixed list quietly stops covering scripts added later.
    """
    found: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*.sh")):
        if not _EXCLUDED_DIRS.isdisjoint(path.relative_to(REPO_ROOT).parts):
            continue
        if _bernstein_argv_lines(path):
            found.append(path)
    return found


SHELL_SCRIPTS: list[Path] = _discover_shell_scripts()

assert SHELL_SCRIPTS, f"no shell scripts invoking the bernstein CLI were discovered under {REPO_ROOT}"


@pytest.mark.parametrize(
    "script",
    SHELL_SCRIPTS,
    ids=[str(p.relative_to(REPO_ROOT)) for p in SHELL_SCRIPTS],
)
def test_shipped_shell_invocations_parse_against_the_real_cli(script: Path) -> None:
    """Every command and option in the script exists on the CLI it targets."""
    failures: list[str] = []
    for lineno, argv in _bernstein_argv_lines(script):
        ok, detail = _resolve(argv)
        if not ok:
            failures.append(f"{script.relative_to(REPO_ROOT)}:{lineno}: {detail}")
    assert not failures, (
        "shipped shell script invokes the bernstein CLI in a way Click rejects "
        "before running anything (exit 2):\n  " + "\n  ".join(failures) + "\n"
        "Cross-check the option against `bernstein <subcommand> --help`; an "
        "option on the root group is not accepted after a subcommand name."
    )


def test_the_action_entrypoint_is_actually_covered() -> None:
    """The extractor must find the Action's own invocations.

    A regex that quietly matches nothing would make every case above pass
    without checking anything, so pin the file this guard exists for and the
    number of executable invocations it contains.
    """
    entrypoint = REPO_ROOT / "action" / "entrypoint.sh"
    assert entrypoint in SHELL_SCRIPTS, "action/entrypoint.sh was not discovered by the scanner"
    invocations = _bernstein_argv_lines(entrypoint)
    assert len(invocations) == 3, (
        f"expected the 3 executable bernstein invocations in {entrypoint.name}, found {len(invocations)}"
    )
    assert any(argv[:1] == ["run"] for _, argv in invocations), "plan mode's `bernstein run` invocation was not found"


def test_the_resolver_rejects_an_option_from_the_wrong_command() -> None:
    """Control: the check has teeth in both directions.

    ``--headless`` is real, but only on the root group. Accepting it after a
    subcommand name is precisely the bug, so a resolver that merged the two
    option sets would pass the suite while shipping a broken Action.
    """
    ok, _ = _resolve(["-g", "some goal", "--budget", "2.00", "--headless"])
    assert ok, "--headless is declared on the root group and must resolve there"

    ok, detail = _resolve(["run", "plan.yaml", "--budget", "2.00", "--headless"])
    assert not ok and "--headless" in detail, "an option not declared on `run` must be rejected after `run`"

    ok, detail = _resolve(["run", "plan.yaml", "--budget", "2.00", "--quiet"])
    assert ok, f"--quiet is declared on `run` and must resolve there, got: {detail}"
