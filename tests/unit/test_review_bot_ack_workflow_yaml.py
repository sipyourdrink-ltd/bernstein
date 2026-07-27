"""Structural assertions on the review-bot acknowledgement workflows
and the gate/sweeper scripts.

These tests pin the contract that:
    * the ``review-bot-ack`` context is published through the Checks
      API and never as a job-name side effect, so no job state can
      write it,
    * the nightly sweeper runs at 06:00 UTC and falls back to
      ``GITHUB_TOKEN`` when ``LANDING_REPO_PAT`` is absent,
    * the classifier maps known bot severity tags into must-address
      vs informational buckets.

The tests are cheap; they parse YAML and exercise the classifier in
isolation. They do not call the GitHub API.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


GATE_WF = Path(".github/workflows/review-bot-ack.yml")
PUBLISH_WF = Path(".github/workflows/review-bot-ack-publish.yml")
SWEEP_WF = Path(".github/workflows/review-bot-sweep.yml")
GATE_SCRIPT = Path("scripts/review_bot_ack.py")
SWEEP_SCRIPT = Path("scripts/review_bot_sweep.py")
PUBLISH_SCRIPT = Path("scripts/publish_required_check.py")

# The context branch protection requires on `main`.
CONTEXT = "review-bot-ack"
PUBLISHER = "scripts/publish_required_check.py"
VERDICT_ARTIFACT = "review-bot-ack-verdict"


@pytest.fixture(scope="module")
def gate_doc() -> dict[str, object]:
    return yaml.safe_load(GATE_WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def publish_doc() -> dict[str, object]:
    return yaml.safe_load(PUBLISH_WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sweep_doc() -> dict[str, object]:
    return yaml.safe_load(SWEEP_WF.read_text(encoding="utf-8"))


def _on(doc: dict[str, object]) -> dict[str, object]:
    # PyYAML 1.1 parses bare ``on:`` as bool True.
    on = doc.get(True, doc.get("on"))
    assert isinstance(on, dict)
    return on


def test_gate_workflow_exists() -> None:
    assert GATE_WF.exists()
    assert GATE_SCRIPT.exists()
    assert PUBLISH_SCRIPT.exists()


def test_gate_triggers(gate_doc: dict[str, object]) -> None:
    on = _on(gate_doc)
    pr = on.get("pull_request")
    assert isinstance(pr, dict)
    types = pr.get("types") or []
    for t in ("opened", "synchronize", "edited"):
        assert t in types, f"gate must trigger on pull_request.{t}"
    assert "pull_request_review" in on


def _jobs(gate_doc: dict[str, object]) -> dict[str, dict]:
    jobs = gate_doc.get("jobs")
    assert isinstance(jobs, dict)
    return {k: v for k, v in jobs.items() if isinstance(v, dict)}


def _steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _run_script(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(job))


def test_no_job_is_named_after_the_required_context(
    gate_doc: dict[str, object],
) -> None:
    """INV-1. A job publishes a check-run named after itself, and that
    check-run inherits the job's fate.

    Branch protection folds every check-run of a required name into its
    verdict, and a later success does not clear an earlier non-success. So
    naming a job `review-bot-ack` turns two ordinary job states into
    permanent blocks on the commit: a `cancelled` instance holds the PR at
    BLOCKED for the life of that SHA (#3042, #3154), and a `skipped`
    instance silently *satisfies* the gate, because GitHub counts skipped
    as passing.

    The context is published through the Checks API instead, so no job
    state can write it.
    """
    named = [key for key, job in _jobs(gate_doc).items() if job.get("name") == CONTEXT]
    assert not named, (
        f"jobs {named} are named after the required context {CONTEXT!r}. A job's check-run inherits the job's "
        "fate, so cancelling it blocks the SHA permanently and skipping it passes the gate without running it. "
        "Publish via scripts/publish_required_check.py and give the job a different name."
    )


def test_every_verdict_path_ends_in_an_api_publish(
    gate_doc: dict[str, object],
    publish_doc: dict[str, object],
) -> None:
    """INV-2. Every job that resolves a verdict must reach an explicit publish.

    A job either publishes the context itself, which needs `checks: write`,
    or hands the verdict to the companion `workflow_run` publisher, which
    publishes on its behalf. Nothing may resolve a verdict and stop.

    The handoff exists because a `pull_request` event raised from a fork
    gets a read-only GITHUB_TOKEN that a `permissions:` block cannot raise,
    so publishing in-job returned 403 for every external contribution and
    left the required context absent.
    """
    jobs = _jobs(gate_doc)
    assert jobs, "workflow must define jobs"
    for key, job in jobs.items():
        script = _run_script(job)
        publishes = PUBLISHER in script
        hands_off = any(VERDICT_ARTIFACT in str((step.get("with") or {}).get("name", "")) for step in _steps(job))
        assert publishes or hands_off, (
            f"job {key!r} resolves a verdict but neither publishes it via {PUBLISHER} nor uploads it as "
            f"the {VERDICT_ARTIFACT!r} artifact for the companion publisher"
        )
        if publishes:
            assert f"--name {CONTEXT}" in script, f"job {key!r} publishes some other context name"
            perms = job.get("permissions")
            assert isinstance(perms, dict) and perms.get("checks") == "write", (
                f"job {key!r} needs checks:write to publish the required context"
            )

    pub_jobs = _jobs(publish_doc)
    assert pub_jobs, "the companion publisher must define a job"
    for key, job in pub_jobs.items():
        script = _run_script(job)
        assert PUBLISHER in script and f"--name {CONTEXT}" in script, (
            f"publisher job {key!r} must publish {CONTEXT!r} via {PUBLISHER}"
        )
        perms = job.get("permissions")
        assert isinstance(perms, dict) and perms.get("checks") == "write", f"publisher job {key!r} needs checks:write"


def test_publisher_runs_on_workflow_run_of_the_gate(
    publish_doc: dict[str, object],
) -> None:
    """INV-2c. The publisher must run in the base context, not the head's.

    `workflow_run` is what makes the token writable for a fork pull
    request. Any other trigger reintroduces the 403.
    """
    on = _on(publish_doc)
    run = on.get("workflow_run")
    assert isinstance(run, dict), "publisher must be triggered by workflow_run"
    assert "Review-bot acknowledgement gate" in (run.get("workflows") or []), (
        "publisher must key off the gate workflow by name"
    )


def test_publisher_pins_the_sha_to_the_event_not_the_artifact(
    publish_doc: dict[str, object],
) -> None:
    """INV-2d. The published SHA comes from the event, never from the fork.

    The verdict artifact is written by a job running on the fork's head, so
    it is attacker-influenced. If the publisher took the SHA from it, a fork
    could publish `success` onto a commit belonging to a different pull
    request. The event's own `head_sha` is not forgeable that way.
    """
    for key, job in _jobs(publish_doc).items():
        for step in _steps(job):
            if PUBLISHER not in str(step.get("run", "")):
                continue
            env = {k: str(v) for k, v in (step.get("env") or {}).items()}
            sha_source = env.get("HEAD_SHA", "")
            assert "workflow_run.head_sha" in sha_source, (
                f"publisher job {key!r} takes the SHA from {sha_source!r}; it must come from "
                "github.event.workflow_run.head_sha so a fork cannot name someone else's commit"
            )


def test_publisher_fails_closed(publish_doc: dict[str, object]) -> None:
    """INV-2e. Anything short of an explicit match publishes failure.

    A missing artifact, an unreadable one, a conclusion that is not the
    literal `success`, a SHA that disagrees with the event, or a gate run
    that did not itself succeed must all resolve to `failure`.
    """
    for key, job in _jobs(publish_doc).items():
        script = _run_script(job)
        if PUBLISHER not in script:
            continue
        assert "verdict=failure" in script, (
            f"publisher job {key!r} must default the verdict to failure before any check succeeds"
        )
        assert "workflow_run.conclusion" in str(job), (
            f"publisher job {key!r} must refuse to publish success when the gate run did not succeed"
        )


def test_direct_publish_never_fails_the_gate_job(
    gate_doc: dict[str, object],
) -> None:
    """INV-2f. The in-job publish must be best effort, never load bearing.

    It is kept for two reasons. `workflow_run` only fires for workflows
    present on the default branch, so a pull request that introduces or
    edits the companion publisher cannot be published by it and could not
    satisfy its own required context. And a same-repo pull request gets the
    context immediately rather than after a second workflow schedules.

    On a fork the token is read-only and this step 403s. If that failure
    propagated, the gate run would conclude `failure`, and the authoritative
    `workflow_run` publisher would then publish `failure` for a pull request
    that actually passed. So the step must carry `continue-on-error`.
    """
    for key, job in _jobs(gate_doc).items():
        for step in _steps(job):
            if PUBLISHER not in str(step.get("run", "")):
                continue
            hands_off = any(VERDICT_ARTIFACT in str((s.get("with") or {}).get("name", "")) for s in _steps(job))
            if not hands_off:
                # A job with no handoff has nothing else to publish for it,
                # so its publish is load bearing and must not be swallowed.
                continue
            assert step.get("continue-on-error") is True, (
                f"job {key!r} both hands the verdict off and publishes directly, so the direct publish must "
                "carry continue-on-error: a fork's read-only token makes it 403, and letting that fail the "
                "job would make the workflow_run publisher report failure for a passing pull request"
            )


def test_no_job_checks_out_the_pull_request_head(
    gate_doc: dict[str, object],
) -> None:
    """INV-2b. The scripts must come from the base branch, never the head.

    Two independent reasons, either one sufficient:

    Availability. ``scripts/publish_required_check.py`` only exists on
    branches cut after it landed. Checking out an older head made the
    publish step die with "can't open file", so the required context was
    never published at all and the pull request sat at BLOCKED with no
    failing required check to point at.

    Security. These jobs hold ``pull-requests: write`` and
    ``issues: write``. Running Python out of a fork's tree with those
    permissions would let the fork author execute arbitrary code against
    this repository.
    """
    for key, job in _jobs(gate_doc).items():
        for step in _steps(job):
            if "checkout" not in str(step.get("uses", "")):
                continue
            ref = str((step.get("with") or {}).get("ref", ""))
            assert "pull_request.head" not in ref, (
                f"job {key!r} checks out the pull request head ({ref!r}). Both scripts reach the pull request "
                "over the API and need nothing from its tree; use the base ref so the publisher always exists "
                "and a fork cannot run code under this job's write permissions."
            )


def test_every_checkout_pins_an_explicit_base_ref(
    gate_doc: dict[str, object],
) -> None:
    """INV-2c. An omitted ``ref:`` is the same defect as an explicit head ref.

    ``test_no_job_checks_out_the_pull_request_head`` only rejects a ref
    that spells the head out. A checkout step with no ``ref:`` at all
    passes it vacuously while doing exactly the thing the docstring
    forbids: ``actions/checkout`` defaults to the ref that triggered the
    run, which on a ``merge_group`` event is the queued entry's own
    ``gh-readonly-queue/...`` branch. That branch carries the candidate
    pull request's tree, so the job runs
    ``scripts/publish_required_check.py`` out of code the pull request
    author controls while holding ``checks: write``.

    ``checks: write`` is enough to forge a required context: it can post
    a completed check-run named ``CI gate`` with conclusion ``success``
    on any commit in the repository. Every job in this workflow reaches
    the pull request over the API and needs nothing from any candidate
    tree, so every checkout here pins the base ref.
    """
    seen = 0
    for key, job in _jobs(gate_doc).items():
        for step in _steps(job):
            if "checkout" not in str(step.get("uses", "")):
                continue
            seen += 1
            ref = str((step.get("with") or {}).get("ref", "")).strip()
            assert ref, (
                f"job {key!r} checks out without an explicit `ref:`. actions/checkout then takes the triggering "
                "ref, which on a merge_group run is the queued entry's own branch, so this job would execute the "
                "candidate pull request's code under its `checks: write` token. Pin the base ref."
            )
            assert "head" not in ref, (
                f"job {key!r} checks out a head ref ({ref!r}); use the base ref so the publisher always exists "
                "and a candidate tree cannot run code under this job's write permissions."
            )
            assert "base" in ref, (
                f"job {key!r} checks out {ref!r}, which is neither the pull request base ref nor the merge "
                "group base ref. Only a base ref is guaranteed to be repository-controlled."
            )
    assert seen, "workflow must check out something; otherwise this guard is vacuous"


def test_publish_is_skipped_while_a_job_is_being_cancelled(
    gate_doc: dict[str, object],
) -> None:
    """INV-3. A cancelled job has no verdict to publish.

    Leaving the context absent is the correct outcome: absent reads as
    BLOCKED, and the next run writes the real verdict. Publishing on the
    way out would write whatever the verdict variable happened to hold.
    """
    for key, job in _jobs(gate_doc).items():
        for step in _steps(job):
            if PUBLISHER not in str(step.get("run", "")):
                continue
            guard = str(step.get("if", ""))
            assert "!cancelled()" in guard, (
                f"the publish step in job {key!r} must be guarded by `if: ${{{{ !cancelled() }}}}`; found {guard!r}"
            )


def test_verdict_defaults_to_failure(gate_doc: dict[str, object]) -> None:
    """INV-4. The gate exits 1 on an open finding and 2 on an internal
    error. Only a clean success may publish success, so the verdict
    variable is initialised to failure and narrowed on an explicit match.
    """
    for key, job in _jobs(gate_doc).items():
        script = _run_script(job)
        assert "verdict=failure" in script, f"job {key!r} does not default its verdict to failure"
        assert 'if [ "$OUTCOME" = "success" ]' in script, (
            f"job {key!r} must publish success only on an explicit success outcome"
        )


def test_gate_concurrency_cancels_like_every_other_pr_workflow(
    gate_doc: dict[str, object],
) -> None:
    """INV-6. Cancellation is safe once the context is API-published.

    The event name must stay OUT of the group key. It never prevented a
    cancellation - with `cancel-in-progress: false` a group is a one-deep
    queue, so a third run in the same group cancels the pending one - and
    splitting the lanes lets a `pull_request` run and a
    `pull_request_review` run for the same commit publish concurrently and
    race on the upsert.
    """
    conc = gate_doc.get("concurrency")
    assert isinstance(conc, dict), "workflow must declare a concurrency block"
    assert conc.get("cancel-in-progress") is True, (
        "the required context no longer depends on a job's fate, so this workflow cancels superseded runs like "
        "every other pull_request workflow"
    )
    group = str(conc.get("group", ""))
    assert "github.event_name" not in group, (
        "splitting the group by event lets the pull_request and pull_request_review lanes run concurrently and "
        "race on the check-run upsert"
    )
    assert "github.event.pull_request.number" in group


def test_merge_group_job_verifies_instead_of_echoing(
    gate_doc: dict[str, object],
) -> None:
    """The merge_group emitter must not satisfy the required context
    unconditionally (#3114). It has to resolve the queued entry's pull
    request and require a successful PR-stage `review-bot-ack` check-run on
    that PR's head commit, failing closed on anything it cannot resolve.
    """
    job = _jobs(gate_doc).get("merge-group-verify")
    assert isinstance(job, dict), "queue-side emitter job must exist"
    assert job.get("name") != CONTEXT, "see test_no_job_is_named_after_the_required_context"
    steps = _steps(job)
    assert steps, "job must have steps"
    scripts = _run_script(job)
    assert "check-runs" in scripts and CONTEXT in scripts, (
        "the queue-side job must query check-runs for the PR-stage gate rather than passing unconditionally (#3114)"
    )
    assert 'select(.status == "completed" and .conclusion == "success")' in scripts, (
        "the queue-side job must require a genuinely successful PR-stage gate; a `skipped` instance counts as "
        "passing for branch protection and must not satisfy this check (#3114)"
    )
    assert "exit 1" in scripts, "the queue-side job must fail closed"
    assert "merge_group.head_ref" in (scripts + "\n".join(str(s.get("env") or {}) for s in steps)), (
        "the queued entry's PR must be resolved from the merge_group ref"
    )
    assert "merge_group.head_sha" in "\n".join(str(s.get("env") or {}) for s in steps), (
        "the queue-side context must be published on the merge group's own head SHA"
    )


def test_gate_actions_sha_pinned() -> None:
    text = GATE_WF.read_text(encoding="utf-8")
    uses = [m.group(0) for m in re.finditer(r"uses:\s*[^\s#]+", text)]
    pat = re.compile(r"uses:\s*[\w./-]+@[0-9a-f]{40}\b")
    for line in uses:
        assert pat.match(line), f"action not SHA-pinned: {line}"


def test_gate_checkout_no_persist_credentials(gate_doc: dict[str, object]) -> None:
    for key, job in _jobs(gate_doc).items():
        checkout = next((s for s in _steps(job) if "checkout" in str(s.get("uses", ""))), None)
        assert checkout is not None, f"job {key!r} must check out the repo to run {PUBLISHER}"
        assert (checkout.get("with") or {}).get("persist-credentials") is False


def test_sweep_workflow_exists() -> None:
    assert SWEEP_WF.exists()
    assert SWEEP_SCRIPT.exists()


def test_sweep_runs_daily_at_06_utc(sweep_doc: dict[str, object]) -> None:
    on = _on(sweep_doc)
    sched = on.get("schedule")
    assert isinstance(sched, list)
    crons = [item.get("cron") for item in sched if isinstance(item, dict)]
    assert "0 6 * * *" in crons, "sweeper must run daily at 06:00 UTC"
    assert "workflow_dispatch" in on


def test_sweep_uses_landing_repo_pat_with_fallback() -> None:
    text = SWEEP_WF.read_text(encoding="utf-8")
    assert "LANDING_REPO_PAT" in text
    assert "GITHUB_TOKEN" in text
    # Both env-binding sites (the script step and the create-pull-request
    # step) must fall back to GITHUB_TOKEN if the PAT is absent.
    assert text.count("LANDING_REPO_PAT || secrets.GITHUB_TOKEN") >= 2


def test_classifier_must_address_vs_informational() -> None:
    sys.path.insert(0, str(Path("scripts").resolve()))
    from review_bot_ack import classify

    must_examples = [
        "**Potential issue**: this is a real bug.",
        "**issue:** missing input validation.",
        "**bug:** infinite loop on empty list.",
        "**security:** credentials logged in plaintext.",
        "**suggestion (security):** sanitise the input.",
    ]
    info_examples = [
        "**Note:** consider renaming this variable.",
        "**suggestion (style):** prefer f-strings here.",
        "_Nit_: trailing whitespace.",
        "**Refactor suggestion**: split this function.",
    ]
    for body in must_examples:
        assert classify(body) == "must-address", body
    for body in info_examples:
        assert classify(body) == "informational", body
