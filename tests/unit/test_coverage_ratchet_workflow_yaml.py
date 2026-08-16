"""Structural assertions on the total-coverage ratchet workflow.

``.github/workflows/coverage-ratchet.yml`` fires when a CI run on ``main``
completes, and opens a pull request carrying the new high-water mark when
that run's measured total coverage exceeds the committed baseline.

The failures this module exists to prevent
------------------------------------------
1. **A per-fire pull request.** The branch name must stay a constant so
   ``peter-evans/create-pull-request`` updates the single open ratchet PR
   in place. A branch name carrying the run id, the SHA or the measured
   percentage would open a fresh PR on every fire.

2. **A PR that can never merge.** A pull request created with
   ``GITHUB_TOKEN`` does not trigger workflows, so the required
   context (``CI gate``) never reports on it. Branch
   protection then holds it at BLOCKED forever while the rollup reads
   SUCCESS, and the only way out is an operator closing it by hand. A
   closed PR cannot be reopened by force-pushing its branch, so the next
   fire has to open a new one - which is what the stable branch above
   looks like it failed to do. The PR-opening step must therefore prefer
   a token that triggers workflows.

3. **A downward move on the open PR.** ``scripts/coverage_ratchet.py``
   compares the measurement against the baseline committed on ``main``,
   not against the one the open ratchet PR already carries. Those diverge
   the moment a ratchet PR is open. A later measurement sitting between
   the two still counts as a bump against ``main``, and force-pushing it
   would rewrite the open PR with a *lower* mark. A guard step must read
   the ratchet branch and gate the PR step on it.

4. **A cancelling concurrency group.** Two fires racing on the same
   branch must serialise, not cancel: a run cancelled between the
   force-push and the PR call leaves a pushed branch with no pull
   request.

5. **A baseline measured on some other commit.** Resolving the coverage
   artifact by "first recent run that has one" accepts a report for a
   commit unrelated to the tree being checked out, so the committed
   high-water mark describes a tree nobody can identify and the next
   honest measurement reads as a drop. Every candidate run must match the
   measured ``head_sha``.

6. **A ratchet that is correct and never fires.** (4) and (5) interact:
   on a ``push`` trigger the ratchet and the CI run start from the same
   event, so the coverage artifact does not exist yet when the ratchet
   looks. Pinning ``head_sha`` without moving the trigger makes every
   commit skip. The trigger must therefore be CI *completion*, and it
   must not narrow to ``conclusion == success`` - ``cancel-in-progress``
   cancels most ``main`` runs, so that filter would idle the ratchet just
   as thoroughly.

7. **A push at a branch the merge queue has locked.** While the ratchet PR
   sits in the queue GitHub rejects every push to its head branch, so
   ``create-pull-request`` hard-fails with ``GH006`` for one full CI matrix
   per queue entry. The guard must therefore read the queue as well as the
   branch baseline, and it must delegate the verdict to
   ``scripts/coverage_ratchet.py guard`` so the rule is unit-tested rather
   than re-implemented in shell.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "coverage-ratchet.yml"

RATCHET_BRANCH = "coverage-ratchet/baseline"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["ratchet"]["steps"]


def _step_by_id(steps: list[dict], step_id: str) -> dict:
    for step in steps:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step with id={step_id!r} in coverage-ratchet.yml")


def _create_pr_step(steps: list[dict]) -> dict:
    for step in steps:
        if "peter-evans/create-pull-request" in str(step.get("uses", "")):
            return step
    raise AssertionError("coverage-ratchet.yml no longer calls create-pull-request")


def test_workflow_file_exists() -> None:
    assert _WORKFLOW.is_file()


def test_branch_name_is_a_constant(steps: list[dict]) -> None:
    """A templated branch name opens one PR per fire instead of updating one."""
    branch = _create_pr_step(steps)["with"]["branch"]
    assert branch == RATCHET_BRANCH
    assert "${{" not in branch, (
        "the ratchet branch must be a constant; a run id / SHA / percentage in "
        "the name makes create-pull-request open a new PR on every fire"
    )


def test_pr_is_opened_with_a_token_that_triggers_workflows(steps: list[dict]) -> None:
    """GITHUB_TOKEN-authored PRs never collect the two required contexts."""
    token = _create_pr_step(steps)["with"]["token"]
    assert "BERNSTEIN_AUTOSYNC_TOKEN" in token, (
        "a PR created with GITHUB_TOKEN does not trigger workflows, so `CI gate` "
        "never reports and the PR can never merge"
    )
    assert token.index("BERNSTEIN_AUTOSYNC_TOKEN") < token.index("GITHUB_TOKEN"), (
        "GITHUB_TOKEN may only be the fallback, never the preferred token"
    )


def test_pr_step_supplies_base_for_the_detached_checkout(steps: list[dict]) -> None:
    """The workflow checks out the measured commit - a detached HEAD - and
    create-pull-request cannot infer a base from a detached HEAD. Without an
    explicit ``base`` the step hard-fails exactly and only on the runs that
    actually bump the baseline: the branch is force-pushed, no PR ever opens,
    and the main round goes red on every genuine coverage increase
    (issue #3434)."""
    assert _create_pr_step(steps)["with"].get("base") == "main", (
        "the ratchet PR's base was always meant to be main - the downward "
        "guard explicitly reasons about the baseline committed on main"
    )


def test_downward_guard_exists_and_reads_the_ratchet_branch(steps: list[dict]) -> None:
    guard = _step_by_id(steps, "guard")
    assert RATCHET_BRANCH in yaml.safe_dump(guard), (
        "the guard must read the baseline carried by the open ratchet branch, not only the one committed on main"
    )
    # The step must still end in something that writes `proceed` to
    # $GITHUB_OUTPUT, or the PR step's `if` is false forever and the ratchet
    # silently never opens a PR. The write itself moved into the script -
    # `test_guard_command_writes_proceed_false_to_github_output` in
    # tests/unit/test_coverage_ratchet.py covers it - so what this asserts
    # here is that the step actually reaches that script.
    assert "coverage_ratchet.py guard" in guard["run"], (
        "the guard step must emit `proceed`; without it the create-pull-request "
        "step is gated on an output nothing ever sets"
    )


def test_pr_step_is_gated_on_the_downward_guard(steps: list[dict]) -> None:
    condition = " ".join(_create_pr_step(steps)["if"].split())
    assert "steps.ratchet.outputs.baseline_bumped == 'true'" in condition
    assert "steps.guard.outputs.proceed == 'true'" in condition, (
        "without the guard a measurement between main's baseline and the open "
        "PR's baseline rewrites the open PR with a lower high-water mark"
    )


def test_guard_runs_only_when_the_ratchet_clicked(steps: list[dict]) -> None:
    assert _step_by_id(steps, "guard")["if"] == "steps.ratchet.outputs.baseline_bumped == 'true'"


def test_concurrency_serialises_and_never_cancels(workflow: dict) -> None:
    concurrency = workflow["concurrency"]
    assert "${{" not in str(concurrency["group"]), (
        "a templated group key gives every push its own group, so two fires can still race on the same branch"
    )
    assert concurrency["cancel-in-progress"] is False, (
        "cancelling between the force-push and the create-pull-request call "
        "leaves a pushed ratchet branch with no pull request"
    )


def test_fires_on_ci_completion_not_on_the_push(workflow: dict) -> None:
    """A `push` trigger races the CI run it wants to measure.

    Both start from the same event, so the resolve step reaches the
    artifact API long before the coverage shard has uploaded. On `push`
    the only way to find *any* artifact is to accept a different commit's
    - exactly the mismatch this workflow exists to prevent. Waiting for CI
    to complete is what lets the head_sha pin hold without idling the
    ratchet forever.
    """
    on = workflow.get("on", workflow.get(True))

    assert "push" not in on, "a push trigger fires before the coverage artifact exists"
    assert on["workflow_run"]["workflows"] == ["CI"]
    assert on["workflow_run"]["branches"] == ["main"]


def test_completion_trigger_is_not_narrowed_to_successful_runs(workflow: dict) -> None:
    """`types: [completed]` must stay unfiltered by conclusion.

    ci.yml's cancel-in-progress concurrency cancels most main runs. A
    trigger that only fired on success would leave the ratchet idle
    almost permanently - the reason the original implementation avoided
    workflow_run altogether.
    """
    on = workflow.get("on", workflow.get(True))

    assert on["workflow_run"]["types"] == ["completed"]


def test_self_retrigger_guard_reads_the_triggering_run(workflow: dict) -> None:
    """On workflow_run the commit lives under the triggering run."""
    guard = workflow["jobs"]["ratchet"]["if"]

    assert "github.event.workflow_run.head_commit.message" in guard, (
        "github.event.head_commit is empty on a workflow_run event, so the guard "
        "would never match and the ratchet would retrigger on its own bump"
    )


def test_privileged_checkout_is_confined_to_commits_from_this_repository(
    workflow: dict,
) -> None:
    """The trigger's `branches:` filter is not a trust boundary.

    This job runs with ``contents: write`` and checks out a commit named
    by the event. ``branches: [main]`` does not confine that to our own
    code: a fork's branch can be called ``main`` as well, and a fork PR
    opened from it produces a CI run carrying that branch name. Only the
    triggering run's repository distinguishes the two.
    """
    guard = workflow["jobs"]["ratchet"]["if"]

    assert (
        "github.event.workflow_run.head_repository.full_name == github.repository" in guard
    ), (
        "without a same-repository guard, a fork PR from a branch named `main` "
        "reaches the privileged checkout below"
    )
    assert "github.event.workflow_run.event == 'push'" in guard, (
        "a pull_request-triggered CI run measures the merge commit of an "
        "unmerged branch; only a push to the default branch may be ratcheted"
    )


# --------------------------------------------------------------------------- #
# the measurement must belong to the commit being checked out
# --------------------------------------------------------------------------- #


def _checkout_step(steps: list[dict]) -> dict:
    for step in steps:
        if "actions/checkout" in str(step.get("uses", "")):
            return step
    raise AssertionError("coverage-ratchet.yml no longer checks out the repo")


def test_checkout_pins_the_measured_commit(steps: list[dict]) -> None:
    """Neither `main` nor `github.sha` is the commit CI measured.

    `main` has usually moved on, and on a workflow_run event `github.sha`
    is the default-branch head rather than the triggering run's commit.
    """
    assert _checkout_step(steps)["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"


def test_run_resolution_targets_the_triggering_run(steps: list[dict]) -> None:
    step = _step_by_id(steps, "ci_run")

    assert step["env"]["TARGET_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert step["env"]["TRIGGERING_RUN"] == "${{ github.event.workflow_run.id }}"
    assert 'has_coverage_artifact "${TRIGGERING_RUN}"' in step["run"], (
        "the run the event handed us is the primary candidate"
    )


def test_sibling_fallback_stays_inside_the_head_sha_filter(steps: list[dict]) -> None:
    """The fallback is deliberate, but it may not reach another commit.

    If the triggering run was cancelled before the coverage shard
    uploaded, another completed run for the SAME commit may still carry a
    usable report. Widening past head_sha would reintroduce the mismatch.
    """
    script = _step_by_id(steps, "ci_run")["run"]

    assert "select(.headSha == env.TARGET_SHA" in script, (
        "the sibling search must be filtered to the measured commit; without it a "
        "coverage artifact from an unrelated commit can justify a baseline bump"
    )
    assert '.status == "completed"' in script, (
        "an in-flight sibling has not finished uploading; only completed runs count"
    )


def test_resolution_never_filters_on_conclusion(steps: list[dict]) -> None:
    """Insisting on a successful run would idle the ratchet permanently."""
    script = _step_by_id(steps, "ci_run")["run"]

    assert 'select(.conclusion == "success")' not in script
    assert "conclusion ==" not in script.replace("TRIGGERING_CONCLUSION", "")


def test_ratchet_step_records_the_resolved_provenance(steps: list[dict]) -> None:
    """The bump has to name the commit and run it came from."""
    step = _step_by_id(steps, "ratchet")

    assert step["env"]["HEAD_SHA"] == "${{ steps.ci_run.outputs.head_sha }}"
    assert step["env"]["RUN_ID"] == "${{ steps.ci_run.outputs.run_id }}"
    assert "--head-sha" in step["run"]
    assert "--run-id" in step["run"]


def test_a_bumped_baseline_is_verified_before_the_pr_opens(steps: list[dict]) -> None:
    """A value that cannot be re-derived must not reach a pull request."""
    verify_steps = [s for s in steps if "coverage_ratchet.py verify" in str(s.get("run", ""))]
    assert verify_steps, "nothing re-derives the bumped baseline before the PR step"

    verify = verify_steps[0]
    assert "--require-provenance" in verify["run"]
    assert verify["if"] == "steps.ratchet.outputs.baseline_bumped == 'true'"
    assert verify.get("continue-on-error") is not True, (
        "an unverifiable baseline must block the PR, not warn and open it anyway"
    )
    assert steps.index(verify) < steps.index(_create_pr_step(steps)), (
        "the verify step must run before create-pull-request"
    )


def test_guard_reads_the_merge_queue_not_only_the_branch_baseline(steps: list[dict]) -> None:
    """A queued ratchet PR locks its branch; the number alone cannot see that."""
    run = _step_by_id(steps, "guard")["run"]
    assert "mergeQueue" in run, (
        "the guard must ask whether the open ratchet PR is in the merge queue - "
        "while it is, every push to its branch is rejected with GH006 whatever "
        "the measured percentage says"
    )
    assert "queued-branches.json" in run


def test_guard_delegates_the_verdict_to_the_tested_script(steps: list[dict]) -> None:
    """One copy of the rule, and it is the copy the unit tests drive.

    An inline re-implementation in shell would drift from
    ``scripts/coverage_ratchet.py guard`` silently: nothing runs this step
    except a live ratchet fire on ``main``.
    """
    run = _step_by_id(steps, "guard")["run"]
    assert "coverage_ratchet.py guard" in run
    assert "--queued-branches" in run
    assert "--open-baseline" in run
