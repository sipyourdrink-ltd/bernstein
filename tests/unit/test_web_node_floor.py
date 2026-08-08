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


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def _web_building_workflows() -> list[tuple[Path, int]]:
    """Workflows that run ``npm`` in ``web/``, paired with the Node major."""
    found: list[tuple[Path, int]] = []
    for path, doc in _workflow_docs():
        steps = _steps(doc)
        runs_npm_in_web = any(
            step.get("working-directory") == "web" and "npm" in str(step.get("run", "")) for step in steps
        )
        if not runs_npm_in_web:
            continue
        versions = [
            str(step.get("with", {}).get("node-version", ""))
            for step in steps
            if "actions/setup-node" in str(step.get("uses", ""))
        ]
        pinned = [v for v in versions if v.strip().split(".")[0].isdigit()]
        assert pinned, f"{path.name} runs npm in web/ without pinning a Node version"
        found.append((path, int(pinned[0].strip().split(".")[0])))
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
