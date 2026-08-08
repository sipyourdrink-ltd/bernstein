"""The Node floor ``web/`` needs, and the Node version CI actually installs.

``web/package.json`` declares a floor because its dependency tree has one:
``react-router@8.3.0`` requires ``>=22.22.0``. Undeclared, that floor is
invisible - a contributor on an older Node gets a failure from whichever
package happens to load first, with nothing naming the cause.

Declaring it only helps while it stays true, which is the failure this file
is aimed at. Both halves drift silently and in opposite directions:

* a dependency bump can raise the real floor above what CI installs, and
  every lane stays green because CI is not the environment that breaks;
* the pinned ``node-version`` can be lowered below the declared floor, and
  nothing reads the declaration to notice.

So the check is that every workflow which runs ``npm`` against ``web/``
installs a Node that satisfies the declared floor. That is verifiable from
the repository alone, with no install step.

What this deliberately does not check is whether the declared floor still
matches the tree - that needs ``node_modules``, and asserting it here would
mean an install in a unit test. It is enforced where the install already
happens: ``npm ci`` warns on an engines mismatch during the freshness gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_PACKAGE_JSON = REPO_ROOT / "web" / "package.json"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _declared_floor() -> tuple[int, int, int]:
    """Parse the ``>=X.Y.Z`` floor, refusing anything this cannot compare.

    A range like ``^22 || >=24`` has no single floor to compare a pinned
    major against, so it fails loudly here rather than being approximated.
    """
    package = json.loads(WEB_PACKAGE_JSON.read_text(encoding="utf-8"))
    spec = package.get("engines", {}).get("node")
    assert isinstance(spec, str) and spec, (
        "web/package.json must declare engines.node - the tree has a floor "
        "(react-router requires >=22.22.0) and an undeclared one is invisible"
    )
    assert spec.startswith(">="), (
        f"engines.node is {spec!r}; this test compares a single `>=X.Y.Z` "
        "floor against the Node CI pins. A compound range needs the "
        "comparison here rewritten, not this assertion relaxed."
    )
    parts = spec[2:].strip().split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), (
        f"engines.node is {spec!r}; expected `>=MAJOR.MINOR.PATCH`"
    )
    major, minor, patch = (int(p) for p in parts)
    return (major, minor, patch)


def _workflow_docs() -> list[tuple[Path, dict[str, Any]]]:
    docs: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            docs.append((path, cast("dict[str, Any]", data)))
    return docs


def _matrix_cells(job: dict[str, Any]) -> list[dict[str, str]]:
    """The ``matrix.<key>`` bindings a job's steps can be expanded against.

    A step field is often an expression rather than a literal - ``web`` shows
    up as ``working-directory: ${{ matrix.package }}`` under a ``package: web``
    cell. Matching the literal alone silently skips those jobs, which is the
    opposite of what a coverage check should do when it does not understand
    something.
    """
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return [{}]

    axes = {
        key: value for key, value in matrix.items() if key not in {"include", "exclude"} and isinstance(value, list)
    }
    cells: list[dict[str, str]] = [{}]
    for key, values in axes.items():
        cells = [{**cell, key: str(value)} for cell in cells for value in values]

    include = matrix.get("include")
    if isinstance(include, list):
        entries = [{str(k): str(v) for k, v in entry.items()} for entry in include if isinstance(entry, dict)]
        # Close enough for a coverage check: an ``include`` entry that does not
        # extend an existing cell stands on its own, which is the case here.
        cells = entries if axes == {} else [*cells, *entries]
    return cells or [{}]


def _resolve(value: Any, cell: dict[str, str]) -> str:
    """Substitute ``${{ matrix.key }}`` with the cell's binding."""
    text = str(value or "")
    for key, bound in cell.items():
        for expression in (f"${{{{ matrix.{key} }}}}", f"${{{{matrix.{key}}}}}"):
            text = text.replace(expression, bound)
    return text


def _node_major(steps: list[Any]) -> int | None:
    versions = [
        str(step.get("with", {}).get("node-version", ""))
        for step in steps
        if isinstance(step, dict) and "actions/setup-node" in str(step.get("uses", ""))
    ]
    pinned = [v for v in versions if v.strip().split(".")[0].isdigit()]
    return int(pinned[0].strip().split(".")[0]) if pinned else None


def _web_building_workflows() -> list[tuple[Path, int]]:
    """Workflows that run ``npm`` in ``web/``, paired with the Node major."""
    found: list[tuple[Path, int]] = []
    for path, doc in _workflow_docs():
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            steps = [step for step in job.get("steps", []) if isinstance(step, dict)]
            builds_web = any(
                _resolve(step.get("working-directory"), cell) == "web" and "npm" in _resolve(step.get("run"), cell)
                for cell in _matrix_cells(job)
                for step in steps
            )
            if not builds_web:
                continue
            major = _node_major(steps)
            assert major is not None, f"{path.name} runs npm in web/ without pinning a Node version"
            found.append((path, major))
            break
    return found


def _workflows_mentioning_web_literally() -> set[Path]:
    """A crude text scan, used only to catch the structural pass under-matching.

    The two methods have to agree. If the resolver above stops recognising a
    lane - a new expression form, a matrix shape it does not expand - it would
    otherwise fail by quietly checking fewer workflows, and every assertion
    would still pass.
    """
    found: set[Path] = set()
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "actions/setup-node" not in text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in {"- package: web", "package: web", "working-directory: web"}:
                found.add(path)
                break
    return found


def test_web_declares_a_node_floor() -> None:
    """An undeclared floor is one nobody can act on."""
    major, _, _ = _declared_floor()
    assert major >= 22, (
        f"declared floor is Node {major}, below what the tree needs (react-router@8.3.0 requires >=22.22.0)"
    )


def test_at_least_one_workflow_builds_web() -> None:
    """If nothing builds `web/` in CI, the next test passes vacuously."""
    assert _web_building_workflows(), (
        "no workflow runs npm in web/; the Node-pin check below would pass by finding nothing rather than by agreeing"
    )


def test_the_structural_scan_finds_every_lane_a_text_scan_can_see() -> None:
    """A resolver that under-matches fails by checking less, which looks green.

    ``typecheck-ts.yml`` reaches ``web`` through ``${{ matrix.package }}``, so
    a matcher comparing the literal string finds only the other lane - and the
    non-vacuity check above still passes, because one lane was found. Two
    independent methods have to agree on the set.
    """
    structural = {path for path, _ in _web_building_workflows()}
    textual = _workflows_mentioning_web_literally()
    missed = textual - structural
    assert not missed, (
        f"{sorted(p.name for p in missed)} name `web` and pin a Node version, "
        "but the structural scan did not find them - so their pin is not "
        "checked against the declared floor. Teach `_matrix_cells` / `_resolve` "
        "the shape they use rather than narrowing this assertion."
    )


def test_ci_installs_a_node_that_satisfies_the_declared_floor() -> None:
    """Otherwise the lane that builds `web/` is not the environment we support."""
    floor_major = _declared_floor()[0]
    for path, pinned_major in _web_building_workflows():
        assert pinned_major >= floor_major, (
            f"{path.name} pins Node {pinned_major}, below the floor "
            f"web/package.json declares (>={floor_major}). Either the pin is "
            "stale or the floor was raised by a dependency bump; CI must run "
            "on a version the package claims to support."
        )
