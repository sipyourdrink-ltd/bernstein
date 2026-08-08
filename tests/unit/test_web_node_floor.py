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

The comparison contract, since "satisfies" is not obvious once a pin is
looser than the floor:

============================  ==========  ==================================
Pin                           Verdict     Why
============================  ==========  ==================================
``24`` (major above floor)    satisfied   cannot resolve below the floor
``22.22.0`` / ``22.30.1``     satisfied   compared as a whole version
``22.0.0``                    fails       same major, below the floor
``22`` (the floor's major)    fails       which 22.x it installs decides it
``lts/*``, ``22.x``, ``>=22`` fails       not resolvable from the tree
============================  ==========  ==================================

Rows four and five fail because they are *unprovable here*, not because they
are known-bad. A pin this cannot reason about is where a silent pass would do
the most damage, so it fails and names itself instead.

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


def _node_pin(steps: list[Any]) -> str | None:
    """The ``node-version`` a job's ``setup-node`` step requests, verbatim."""
    versions = [
        str(step.get("with", {}).get("node-version", "")).strip()
        for step in steps
        if isinstance(step, dict) and "actions/setup-node" in str(step.get("uses", ""))
    ]
    present = [version for version in versions if version]
    return present[0] if present else None


def _pin_satisfies(pin: str, floor: tuple[int, int, int]) -> tuple[bool, str]:
    """Whether *pin* provably installs a Node at or above *floor*.

    The contract is deliberately narrow, because the interesting failure is a
    pin that *might* resolve below the floor:

    * a pin whose major exceeds the floor's satisfies it whatever it resolves
      to - ``24`` cannot produce a ``22.x``;
    * a pin on the floor's own major has to state minor and patch. ``22``
      means "newest 22.x", which is only above ``22.22.0`` as a fact about
      what upstream has released, not one this repository can check. ``22.0.0``
      is below the floor outright;
    * anything not of the form ``MAJOR[.MINOR.PATCH]`` - ``lts/*``, ``node``,
      ``22.x``, a range, a ``.nvmrc`` indirection - is not provable here.

    Unprovable is reported as unsatisfied. A pin this cannot reason about is
    exactly the case where a silent pass would be worst.
    """
    parts = pin.split(".")
    if not all(part.isdigit() for part in parts) or len(parts) not in {1, 3}:
        return False, (
            f"{pin!r} is not an exact `MAJOR` or `MAJOR.MINOR.PATCH` pin, so it "
            "cannot be shown to satisfy the floor. Pin an exact version, or "
            "teach this check the form you need."
        )

    numbers = tuple(int(part) for part in parts)
    if numbers[0] > floor[0]:
        return True, ""
    if numbers[0] < floor[0]:
        return False, f"Node {pin} is below the declared floor"
    if len(numbers) == 1:
        return False, (
            f"{pin!r} pins only the major that the floor itself sits on, so which "
            "release it installs decides whether the floor holds - state minor and "
            "patch, or raise the pin to a higher major"
        )
    return (numbers >= floor), ("" if numbers >= floor else f"Node {pin} is below the declared floor")


def _web_building_workflows() -> list[tuple[Path, str]]:
    """Workflows that run ``npm`` in ``web/``, paired with their Node pin."""
    found: list[tuple[Path, str]] = []
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
            pin = _node_pin(steps)
            assert pin is not None, f"{path.name} runs npm in web/ without pinning a Node version"
            found.append((path, pin))
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
    floor = _declared_floor()
    for path, pin in _web_building_workflows():
        satisfied, why = _pin_satisfies(pin, floor)
        assert satisfied, (
            f"{path.name} pins Node {pin!r} against a declared floor of "
            f">={'.'.join(str(part) for part in floor)}: {why}. Either the pin is "
            "stale or the floor was raised by a dependency bump; CI must run on a "
            "version the package claims to support."
        )


@pytest.mark.parametrize(
    ("pin", "satisfied"),
    [
        ("24", True),  # a higher major cannot resolve below the floor
        ("22.22.0", True),  # exactly the floor
        ("22.30.1", True),
        ("23.0.0", True),
        ("22.0.0", False),  # same major, below the floor - the case majors miss
        ("22.21.9", False),
        ("21", False),
        ("20.19.0", False),
        ("22", False),  # floor's own major, unpinned minor: not provable
        ("lts/*", False),  # not provable
        ("22.x", False),
        ("node", False),
        (">=22", False),
        ("", False),
    ],
)
def test_the_pin_comparison_uses_the_whole_version(pin: str, *, satisfied: bool) -> None:
    """Comparing majors alone accepts `22.0.0` against a `>=22.22.0` floor.

    The unprovable cases are asserted as unsatisfied on purpose. A pin this
    cannot reason about is where a silent pass would do the most damage, so it
    fails and names itself rather than being waved through.
    """
    assert _pin_satisfies(pin, (22, 22, 0))[0] is satisfied
