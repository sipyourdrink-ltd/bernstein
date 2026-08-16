"""Trigger and header assertions for the adapter contract drift workflow.

Covers ``.github/workflows/adapter-contract-drift.yml``.

The per-PR trigger was dropped on purpose. The matrix is 16 jobs per fire
against the same runner pool that pull-request verdicts queue behind, so
pre-merge validation moved to the merge queue's ephemeral branch and the
daily cron kept surfacing upstream CLI releases. Two things can undo that
quietly:

* someone adds ``pull_request`` back to make one branch validate sooner,
  and the CI bill returns without the decision being revisited;
* the trigger list changes and the comment block above ``on:`` does not,
  leaving the file's own description of when it runs wrong - which is how
  the header came to advertise a PR trigger that had already been removed.

So both halves are pinned here: the exact set of triggers, and the claim
the header makes about them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "adapter-contract-drift.yml"

#: Every trigger this workflow may declare, and nothing else.
#:
#: ``merge_group`` is the pre-merge lane, ``schedule`` catches upstream CLI
#: releases independent of any merge, and ``workflow_dispatch`` covers an
#: operator re-check. ``pull_request`` is absent by decision, not omission.
EXPECTED_TRIGGERS = frozenset({"schedule", "workflow_dispatch"})

#: A header bullet, e.g. ``#   * Manual dispatch for operator-driven re-runs.``
BULLET = re.compile(r"^#\s+\*\s+(?P<text>.*)$")

#: A claim that this workflow runs per pull request.
#:
#: Matched against the normalised bullet text, so ``pull_request``,
#: ``pull-request`` and ``pull request`` all reach this as one spelling.
PR_CLAIM = re.compile(r"\bpull requests?\b|\bprs?\b")


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _triggers() -> dict[str, Any]:
    doc = _doc()
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    return cast("dict[str, Any]", triggers)


def _header() -> list[str]:
    """The comment lines above the ``on:`` key."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    header = []
    for line in lines:
        if line.startswith("on:"):
            break
        header.append(line)
    else:  # pragma: no cover - a workflow with no triggers fails earlier
        raise AssertionError(f"{WORKFLOW.name} has no top-level `on:` key")
    return header


def _trigger_bullets() -> list[str]:
    """The bullets under the header's ``Triggers:`` heading.

    Scoped to that section on purpose: the prose above it discusses the
    aggregator's issue handling and the absence of a batched auto-PR, so a
    search for "PR" across the whole header would answer about the wrong
    sentence. Wrapped continuation lines are folded into the bullet they
    belong to.
    """
    lines = _header()
    start = next((i for i, line in enumerate(lines) if line.strip() == "# Triggers:"), None)
    assert start is not None, "the header must document when this workflow runs, under `# Triggers:`"

    bullets: list[str] = []
    for line in lines[start + 1 :]:
        match = BULLET.match(line)
        if match is not None:
            bullets.append(match.group("text").strip())
            continue
        if not line.startswith("#"):
            break
        continuation = line.lstrip("#").strip()
        if bullets and continuation:
            bullets[-1] = f"{bullets[-1]} {continuation}"
    assert bullets, "the `Triggers:` section must list the triggers"
    return bullets


def _normalise(text: str) -> str:
    """Lowercase, with ``_`` and ``-`` read as spaces.

    ``merge_group``, ``merge-queue`` and ``merge group`` are the same claim
    written three ways; the assertions below should not care which one the
    header happens to use.
    """
    return re.sub(r"[_\-]+", " ", text).lower()


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_the_trigger_set_is_exactly_the_one_that_was_decided() -> None:
    """Adding ``pull_request`` back is the regression this locks out."""
    declared = frozenset(_triggers())
    assert declared == EXPECTED_TRIGGERS, (
        f"triggers are {sorted(declared)}, expected {sorted(EXPECTED_TRIGGERS)}. "
        "Both per-change triggers were dropped to cut CI load - the matrix is "
        "16 jobs per fire against the pool that PR verdicts queue behind, and "
        "only `CI gate` is required in the merge queue, so this lane never "
        "gated a queue merge while competing with ci.yml on every entry and "
        "reshuffle. Upstream drift is a function of upstream releases, not of "
        "what a change touches. If either needs to come back, say so here and "
        "in the header comment rather than letting the file and its "
        "description disagree"
    )


def test_no_pull_request_trigger() -> None:
    """The exact-set assertion states this; this one names it."""
    assert "pull_request" not in _triggers(), (
        "pre-merge validation runs on the merge queue's ephemeral branch, not on every pull-request push"
    )


def test_the_header_does_not_advertise_a_trigger_that_was_removed() -> None:
    """The header outlived the trigger once already."""
    offenders = [bullet for bullet in _trigger_bullets() if PR_CLAIM.search(_normalise(bullet))]
    assert not offenders, (
        f"the header still describes a pull-request trigger: {offenders}. "
        "This workflow does not run per pull request; a comment that says "
        "otherwise is what sends someone looking for a check that never fires"
    )


def test_the_header_describes_the_merge_queue_trigger() -> None:
    """A trigger nobody documented is a trigger nobody expects to pay for."""
    bullets = _trigger_bullets()
    assert any("merge group" in _normalise(bullet) or "merge queue" in _normalise(bullet) for bullet in bullets), (
        f"no header bullet mentions the merge-queue trigger: {bullets}. "
        "`merge_group` has no paths filter, so the full 16-job matrix runs "
        "pre-merge - the header is where that is stated"
    )


@pytest.mark.parametrize(
    "bullet",
    [
        pytest.param("On PRs touching adapters or contracts.", id="prs-abbreviated"),
        pytest.param("On pull requests touching adapters.", id="pull-requests-spelled"),
        pytest.param("On pull_request for immediate validation.", id="event-name"),
        pytest.param("On every PR push.", id="pr-singular"),
    ],
)
def test_a_reintroduced_pr_claim_is_caught_however_it_is_spelled(bullet: str) -> None:
    """The check is on the claim, not on one wording of it."""
    assert PR_CLAIM.search(_normalise(bullet)), f"{bullet!r} claims a PR trigger and must be rejected"


@pytest.mark.parametrize(
    "bullet",
    [
        pytest.param("On merge group for pre-merge adapter contract validation.", id="merge-group"),
        pytest.param("On merge_group, the pre-merge lane.", id="event-name"),
        pytest.param("On the merge-queue branch before a merge.", id="merge-queue"),
    ],
)
def test_the_merge_queue_trigger_is_recognised_however_it_is_spelled(bullet: str) -> None:
    """Normalisation is what lets one assertion accept all three spellings."""
    normalised = _normalise(bullet)
    assert "merge group" in normalised or "merge queue" in normalised
    assert not PR_CLAIM.search(normalised), "a merge-queue bullet must not read as a PR claim"
