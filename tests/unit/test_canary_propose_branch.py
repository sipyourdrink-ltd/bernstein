"""The canary proposal branch must carry only what the canary regenerates.

Covers ``scripts/canary_propose_branch.py``, which exists because the workflow
step used to build its branch off the *workflow checkout* rather than off the
default branch as it stands at commit time. The checkout is taken before the
matrix probes every adapter, and a night that regenerates nothing exits before
pushing, so the long-lived ``bot/adapter-canary-last-green`` branch drifts
behind ``main``. Anything merged into ``main`` in that window then appears in
the proposal as a revert - #4496 caught ``docs/security/receipt-format-spec.md``
being reverted to a pre-#4489 form, which a squash merge would have shipped.

Reading the workflow cannot show that: the ``git add`` line was already
correct, and the revert arrived through the branch's *base*. So the drift is
reproduced here against real git repositories - a clone whose origin has moved
on since it was taken - and the assertion is on the resulting changed-file set,
not on the YAML.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from canary_propose_branch import (
    PROJECTION_PATHS,
    ProposalError,
    build_proposal,
)

LAST_GREEN, CANARY_DOC = PROJECTION_PATHS
#: A file the canary never regenerates, standing in for #4496's real victim.
UNRELATED = "docs/security/receipt-format-spec.md"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def drifted_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A clone taken before ``origin/main`` gained an unrelated commit.

    Returns ``(clone, origin)``. The clone still has the regenerated
    projection in its working tree, exactly as the workflow leaves it after
    ``--update-docs``; origin has since moved on.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "canary@example.invalid")
    _git(origin, "config", "user.name", "Canary Test")
    _write(origin, LAST_GREEN, '{"adapters": {}}\n')
    _write(origin, CANARY_DOC, "# conformance canary\n\nold table\n")
    _write(origin, UNRELATED, "original spec\n")
    _commit_all(origin, "seed")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "canary@example.invalid")
    _git(clone, "config", "user.name", "Canary Test")

    # main moves on after the clone was taken - this is the drift window.
    _write(origin, UNRELATED, "spec edited by another PR\n")
    _commit_all(origin, "docs: edit the spec the canary never touches")

    # ...and the canary regenerates its projection in the stale checkout.
    _write(clone, LAST_GREEN, '{"adapters": {"claude": "green"}}\n')
    _write(clone, CANARY_DOC, "# conformance canary\n\nfresh table\n")
    return clone, origin


def test_proposal_carries_only_the_regenerated_paths(drifted_clone: tuple[Path, Path]) -> None:
    """The unrelated file must not appear, as a revert or otherwise."""
    clone, _ = drifted_clone

    changed = build_proposal(clone, branch="bot/adapter-canary-last-green", base_ref="main")

    assert changed == tuple(sorted(PROJECTION_PATHS))
    assert UNRELATED not in changed


def test_proposal_base_is_the_fetched_default_branch(drifted_clone: tuple[Path, Path]) -> None:
    """Rebuilding off origin/main keeps the newer commit in the branch's history.

    This is the property that stops the revert: if the branch were still based
    on the stale checkout, origin's later commit would be absent from its
    ancestry and would read as a deletion in the diff.
    """
    clone, origin = drifted_clone
    head_after_drift = _git(origin, "rev-parse", "main")

    build_proposal(clone, branch="bot/adapter-canary-last-green", base_ref="main")

    ancestry = _git(clone, "rev-list", "HEAD")
    assert head_after_drift in ancestry.splitlines()
    assert (clone / UNRELATED).read_text(encoding="utf-8") == "spec edited by another PR\n"


def test_stray_path_fails_instead_of_being_dropped(drifted_clone: tuple[Path, Path]) -> None:
    """A staged path outside the projection stops the proposal.

    Staged changes survive ``git checkout -B``, so anything an earlier step
    left in the index rides into the proposal even though this script only
    adds the projection paths itself. That is the shape of #4496 - a path the
    canary never regenerates reaching review - so the check asserts the whole
    staged set against the merge base rather than trusting its own ``git add``.

    Failing is deliberate. Dropping the path with a warning would let a real
    projection bug leave the working tree unnoticed, which is the class of
    fault this check exists to surface.
    """
    clone, _ = drifted_clone
    _write(clone, UNRELATED, "staged by some other step\n")
    _git(clone, "add", UNRELATED)

    with pytest.raises(ProposalError) as excinfo:
        build_proposal(clone, branch="bot/adapter-canary-last-green", base_ref="main")

    assert UNRELATED in str(excinfo.value)


def test_unchanged_projection_proposes_nothing(drifted_clone: tuple[Path, Path]) -> None:
    """A night that regenerates nothing must not manufacture a commit."""
    clone, _origin = drifted_clone
    # Put the projection back to what origin already has.
    _write(clone, LAST_GREEN, '{"adapters": {}}\n')
    _write(clone, CANARY_DOC, "# conformance canary\n\nold table\n")

    changed = build_proposal(clone, branch="bot/adapter-canary-last-green", base_ref="main")

    assert changed == ()
