"""The manifest this repository commits about itself.

Bernstein is the volunteer program's first project, so `.bernstein/volunteer.json`
is not a fixture -- it is a live policy that donors will run against. Two ways it
can quietly become false, and one test each:

*It stops loading.* A manifest that fails validation does not fail loudly; the
project simply stops rendering as joinable, and nobody finds out until someone
asks why the dogfood project vanished from discovery.

*It drifts from the bar CI actually enforces.* Declaring a gate CI does not run
is a promise to volunteers that their passing submission will be accepted, and
omitting one CI does run sends them to write a patch that will be rejected on
arrival. Either direction wastes a stranger's evening, which is the one currency
this program cannot print.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.volunteer import (
    VOLUNTEER_MANIFEST_PATH,
    canonical_manifest_bytes,
    load_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / VOLUNTEER_MANIFEST_PATH
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Trees a volunteer patch may touch. Everything outside them is either a
#: supply-chain surface (`.github/`, `pyproject.toml`, `uv.lock`) or operator
#: tooling that runs with the maintainer's own credentials (`scripts/`).
SAFE_ROOTS = frozenset({"src", "tests", "docs"})


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_this_projects_own_manifest_loads(manifest) -> None:
    """The first project must not silently drop out of its own program."""
    assert manifest.version == 1
    assert manifest.license == "Apache-2.0"
    assert manifest.gates, "a project with no gates accepts anything"


def test_the_declared_license_is_the_license_the_repository_ships(manifest) -> None:
    """The preflight compares the two; disagreeing here fails every submission."""
    licence_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in licence_text
    assert "Version 2.0" in licence_text


def test_the_lint_gates_are_the_commands_ci_runs(manifest) -> None:
    """Declared word for word, not paraphrased.

    A gate is re-run clean-room before a submission is accepted (#3871), so
    ``ruff check src/`` and ``ruff check .`` are different promises even though
    they look interchangeable.
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    ci_runs = {line.split("run:", 1)[1].strip() for line in workflow.splitlines() if "run:" in line}

    for gate in manifest.gates:
        if "ruff" not in gate.argv:
            continue
        assert " ".join(gate.argv) in ci_runs, (
            f"manifest declares {' '.join(gate.argv)!r}, which no CI step runs; "
            "a volunteer passing it would still be rejected on arrival"
        )


def test_the_test_gate_is_this_projects_own_runner(manifest) -> None:
    """`scripts/run_tests.py`, not pytest.

    The repository runs its suite through an isolated per-file runner because a
    single bare pytest process over the whole tree exhausts the disk. A volunteer
    told to run pytest directly would fill their own machine, which is a poor
    introduction to donating it.
    """
    test_gates = [g for g in manifest.gates if any("run_tests.py" in part for part in g.argv)]
    assert len(test_gates) == 1, "exactly one test gate, and it is the repository's runner"
    assert not any(g.argv[:1] == ["pytest"] or "pytest" in g.argv for g in manifest.gates)


def test_mypy_is_not_declared_as_a_gate(manifest) -> None:
    """CI runs mypy advisory (`|| true`), so declaring it would overstate the bar.

    The tempting mistake is to list every quality command the project owns.
    A gate a volunteer must pass but the project does not itself enforce turns a
    mergeable patch into a rejected one for a reason no maintainer would defend.
    """
    assert not any("mypy" in part for gate in manifest.gates for part in gate.argv)


def test_a_volunteer_patch_cannot_reach_the_supply_chain(manifest) -> None:
    """The security property, stated as roots rather than as a denylist.

    Asserting on the roots rather than on forbidden paths means a future pattern
    nobody anticipated -- `.github/**`, `*.toml`, `uv.lock` -- fails this test by
    construction instead of slipping past an enumeration.

    Why it matters: a patch that can edit `.github/workflows/` executes with the
    repository's own secrets the moment it merges, and one that can edit
    `pyproject.toml` or `uv.lock` adds a dependency that runs on every machine
    afterwards. Neither is a code-review problem; both are outside what review
    reliably catches.
    """
    assert manifest.allowed_paths, "an empty allowed_paths means repo-wide, which this project does not offer"

    roots = {pattern.split("/", 1)[0] for pattern in manifest.allowed_paths}
    assert roots <= SAFE_ROOTS, f"patterns rooted outside {sorted(SAFE_ROOTS)}: {sorted(roots - SAFE_ROOTS)}"


def test_egress_is_empty_so_the_deny_all_default_keeps_its_meaning(manifest) -> None:
    """Package registries come from the sandbox profile; this project adds nothing.

    Every host added here is a host a task's output can be posted to.
    """
    assert manifest.egress_allowlist == ()


def test_the_wall_clock_fits_the_suite_it_asks_volunteers_to_run(manifest) -> None:
    """A ceiling below the gate's own runtime kills every task at the finish line."""
    assert 30 <= manifest.max_wall_clock_minutes <= 120


def test_reformatting_the_committed_file_does_not_change_its_digest(manifest) -> None:
    """Outstanding receipts survive a re-indent of the file they are bound to."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    reformatted = json.dumps(raw, indent=8, sort_keys=True)

    assert load_manifest(reformatted).digest == manifest.digest
    assert canonical_manifest_bytes(load_manifest(reformatted)) == canonical_manifest_bytes(manifest)


def test_the_committed_file_is_where_every_consumer_looks(manifest) -> None:
    """Discovery, the sandbox, the runner and the verifier all read one path."""
    assert MANIFEST_PATH.relative_to(REPO_ROOT).as_posix() == VOLUNTEER_MANIFEST_PATH
