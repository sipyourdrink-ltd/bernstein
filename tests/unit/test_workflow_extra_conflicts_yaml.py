"""``uv sync --all-extras`` must not request a declared-conflicting extra pair.

``[tool.uv].conflicts`` in ``pyproject.toml`` makes some extras mutually
exclusive. ``uv sync --all-extras`` asks for every extra at once, so any
workflow using it plainly fails at resolution time with
``Extras ... are incompatible with the declared conflicts`` and the job never
reaches its tests. This test ties the two together: adding a conflict pair
without excluding one side from an ``--all-extras`` step is a resolution
failure, and it should be caught here rather than in CI minutes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# `uv sync ... --all-extras ...` on a single line, capturing the whole command.
_ALL_EXTRAS = re.compile(r"uv\s+sync\b[^\n]*--all-extras\b[^\n]*")
_NO_EXTRA = re.compile(r"--no-extra[= ]([A-Za-z0-9_.-]+)")


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


def _all_extras_commands() -> list[tuple[Path, str]]:
    """Return every ``(workflow, command)`` pair that syncs all extras."""
    found: list[tuple[Path, str]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        found.extend((path, match.group(0)) for match in _ALL_EXTRAS.finditer(text))
    return found


def test_conflict_groups_are_declared() -> None:
    """The guard below is only meaningful while a conflict is declared."""
    assert _conflict_groups(), "pyproject declares no [tool.uv] conflicts; drop this guard"


def test_all_extras_syncs_exclude_one_side_of_every_conflict() -> None:
    """Each ``--all-extras`` step must exclude all but one extra per conflict group."""
    groups = _conflict_groups()
    offenders: list[str] = []

    for path, command in _all_extras_commands():
        excluded = set(_NO_EXTRA.findall(command))
        for group in groups:
            remaining = group - excluded
            if len(remaining) > 1:
                offenders.append(f"{path.name}: {command.strip()} -> requests conflicting extras {sorted(remaining)}")

    assert not offenders, "uv sync --all-extras requests a declared-conflicting extra pair:\n" + "\n".join(offenders)
