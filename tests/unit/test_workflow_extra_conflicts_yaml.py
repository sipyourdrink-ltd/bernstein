"""``uv sync --all-extras`` must not request a declared-conflicting extra pair.

``[tool.uv].conflicts`` in ``pyproject.toml`` makes some extras mutually
exclusive. ``uv sync --all-extras`` asks for every extra at once, so any
workflow using it plainly fails at resolution time with
``Extras ... are incompatible with the declared conflicts`` and the job never
reaches its tests. This test ties the two together: adding a conflict pair
without excluding one side from an ``--all-extras`` step is a resolution
failure, and it should be caught here rather than in CI minutes.

The scan reads the workflow as YAML and looks only at ``run`` values, then
tokenizes each command with :mod:`shlex`. Scanning raw file text instead would
miss a command split across lines with a backslash -- the case a guard exists
for -- and would flag ``echo "uv sync --all-extras"`` or a commented-out line,
which are not steps at all.
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

#: Shell operators that end one logical command inside a ``run`` block.
_COMMAND_SPLIT = re.compile(r"&&|\|\||[;\n|]")
#: A backslash-continued newline: the two lines are one command.
_CONTINUATION = re.compile(r"\\\s*\n\s*")


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


def _commands(run_value: str) -> Iterator[list[str]]:
    """Yield the token list of each logical command in one ``run`` value."""
    joined = _CONTINUATION.sub(" ", run_value)
    for fragment in _COMMAND_SPLIT.split(joined):
        stripped = fragment.strip()
        if not stripped:
            continue
        try:
            # ``comments=True`` drops a trailing ``# ...`` outside quotes, so a
            # commented-out flag is not read as if it were passed.
            tokens = shlex.split(stripped, comments=True)
        except ValueError:
            # Unbalanced quoting -- most often a ``${{ }}`` expression. Skipping
            # is the safe direction: the guard reports fewer commands, never a
            # command it misread.
            continue
        if tokens:
            yield tokens


def _syncs_all_extras(tokens: list[str]) -> bool:
    """Whether this command is a ``uv sync`` requesting every extra."""
    if "--all-extras" not in tokens:
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


def _all_extras_commands() -> list[tuple[Path, list[str]]]:
    """Return every ``(workflow, tokens)`` pair that syncs all extras."""
    found: list[tuple[Path, list[str]]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        for run_value in _run_values(loaded):
            found.extend((path, tokens) for tokens in _commands(run_value) if _syncs_all_extras(tokens))
    return found


def test_conflict_groups_are_declared() -> None:
    """The guard below is only meaningful while a conflict is declared."""
    assert _conflict_groups(), "pyproject declares no [tool.uv] conflicts; drop this guard"


def test_all_extras_syncs_exclude_one_side_of_every_conflict() -> None:
    """Each ``--all-extras`` step must exclude all but one extra per conflict group."""
    groups = _conflict_groups()
    offenders: list[str] = []

    for path, tokens in _all_extras_commands():
        excluded = _excluded_extras(tokens)
        for group in groups:
            remaining = group - excluded
            if len(remaining) > 1:
                offenders.append(
                    f"{path.name}: {shlex.join(tokens)} -> requests conflicting extras {sorted(remaining)}"
                )

    assert not offenders, "uv sync --all-extras requests a declared-conflicting extra pair:\n" + "\n".join(offenders)


def test_scan_reads_continuations_quotes_and_comments() -> None:
    """The scan itself: the three shapes a raw-text scan gets wrong."""
    # A command split across lines is still one command.
    continued = "uv sync --frozen \\\n  --all-extras --no-extra modal\n"
    tokens = [t for t in _commands(continued) if _syncs_all_extras(t)]
    assert len(tokens) == 1
    assert _excluded_extras(tokens[0]) == {"modal"}

    # A quoted value is the same exclusion as a bare one.
    assert _excluded_extras(next(_commands('uv sync --all-extras --no-extra "modal"'))) == {"modal"}
    assert _excluded_extras(next(_commands("uv sync --all-extras --no-extra=modal"))) == {"modal"}

    # Text that merely mentions the command is not the command.
    assert not [t for t in _commands('echo "uv sync --all-extras"') if _syncs_all_extras(t)]
    assert not [t for t in _commands("# uv sync --all-extras") if _syncs_all_extras(t)]


def test_scan_ignores_yaml_outside_run_steps() -> None:
    """A ``--all-extras`` string somewhere other than a ``run`` is not a step."""
    workflow = {"jobs": {"a": {"steps": [{"name": "uv sync --all-extras", "uses": "actions/checkout@v4"}]}}}
    assert list(_run_values(workflow)) == []
