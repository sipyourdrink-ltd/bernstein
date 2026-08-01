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

import itertools
import json
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
CURRENCY = "scripts/ack_publisher_currency.py"
VERDICT_ARTIFACT = "review-bot-ack-verdict"

# The gate triggers on these events for a pull request. Every one of them
# gets a read-only GITHUB_TOKEN on a fork, so every one of them needs the
# `workflow_run` hop to publish. `merge_group` is excluded on purpose: those
# runs already hold `checks: write` and publish in-repo.
GATE_EVENTS_NEEDING_THE_HOP = ("pull_request", "pull_request_review")

# Every conclusion GitHub can hand a completed workflow run.
RUN_CONCLUSIONS = (
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "startup_failure",
    "skipped",
    "neutral",
    "action_required",
)


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


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9.]*")
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_PY_KEYWORDS = frozenset({"and", "or", "not", "None", "True", "False"})


def _eval_condition(expr: str, context: dict[str, str | None]) -> bool:
    """Evaluate the slice of the GitHub expression language these job
    conditions are written in: context lookups, single-quoted strings,
    ``==``, ``!=``, ``&&``, ``||`` and parentheses.

    The point is to read a job's ``if:`` as the predicate it actually is
    rather than to grep it for substrings. A substring assertion cannot
    tell whether two conditions between them cover every gate run; this
    can.
    """
    text = " ".join(expr.split())
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    # Longest path first so `...workflow_run.conclusion` is not clipped by a
    # shorter prefix that happens to also be in the context.
    for path, value in sorted(context.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(path, repr(value))
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"\btrue\b", "True", text)
    text = re.sub(r"\bfalse\b", "False", text)

    # Identifiers are only interesting outside string literals; the literals
    # themselves are values, not names the model has to know.
    bare = _STRING_LITERAL_RE.sub(" ", text)
    unknown = sorted({t for t in _IDENTIFIER_RE.findall(bare) if t not in _PY_KEYWORDS})
    assert not unknown, (
        f"condition {expr!r} reads {unknown!r}, which this test's model of the publisher does not know about. "
        "Either add it to the model or express the condition in terms the model already covers - an untestable "
        "condition is how #3313 stayed invisible."
    )
    # The grammar is restricted to the tokens asserted above, and the input
    # is this repository's own workflow file.
    return bool(eval(text, {"__builtins__": {}}, {}))


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
    publishing_jobs = [key for key, job in pub_jobs.items() if PUBLISHER in _run_script(job)]
    assert publishing_jobs, f"the companion publisher must have a job that runs {PUBLISHER}"
    for key, job in pub_jobs.items():
        script = _run_script(job)
        if PUBLISHER in script:
            assert f"--name {CONTEXT}" in script, f"publisher job {key!r} must publish {CONTEXT!r} via {PUBLISHER}"
            perms = job.get("permissions")
            assert isinstance(perms, dict) and perms.get("checks") == "write", (
                f"publisher job {key!r} needs checks:write"
            )
            continue
        # A job that does not publish must be the recovery path: the only
        # other reason to exist in this workflow is to get a head that has
        # no writer left back into a state where one runs (#3313).
        assert "/rerun" in script, (
            f"job {key!r} in the publisher neither publishes {CONTEXT!r} nor re-dispatches the gate. Those are "
            "the only two things this workflow does; anything else is a verdict path that stops halfway."
        )


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


def test_publisher_never_speaks_for_a_cancelled_gate_run(
    publish_doc: dict[str, object],
) -> None:
    """INV-2g. A run with no verdict must not write one.

    A `cancelled` gate run did not decide against the pull request; it was
    superseded before it could decide anything. `review-bot-ack.yml` already
    refuses to publish from a cancelled job for that reason, and the publisher
    has to hold the same line or the problem simply moves one hop later.

    Treating `cancelled` as `failure` is not conservative here, it is wrong,
    and it is wrong in the direction that blocks good work: it turns a passing
    pull request red with a summary describing a run that never finished.

    The recovery job added for #3313 does run for a cancelled gate run, and
    that is not a weakening of this rule: it writes no verdict at all, it
    re-dispatches the gate so that a run with a verdict exists.
    """
    for key, job in _jobs(publish_doc).items():
        if PUBLISHER not in _run_script(job):
            continue
        condition = " ".join(str(job.get("if", "")).split())
        assert "conclusion != 'cancelled'" in condition, (
            f"publisher job {key!r} runs for a cancelled gate run; its condition is "
            f"{condition!r}. A cancelled run has no verdict and must publish nothing."
        )


def test_the_recovery_job_never_writes_a_verdict(
    publish_doc: dict[str, object],
) -> None:
    """INV-2j. The job that runs for a cancelled gate run must not publish.

    It exists because a cancelled run leaves the head with no writer, and
    the remedy is to produce a run that has a verdict - not to invent one.
    Handing it `checks: write` would let a run with no verdict write one,
    which is the exact failure `test_publisher_never_speaks_for_a_cancelled_gate_run`
    forbids.
    """
    for key, job in _jobs(publish_doc).items():
        script = _run_script(job)
        if PUBLISHER in script:
            continue
        perms = job.get("permissions")
        assert isinstance(perms, dict), f"recovery job {key!r} must pin explicit permissions"
        assert perms.get("checks") != "write", (
            f"recovery job {key!r} holds checks:write. It runs for gate runs that have no verdict, so it must "
            "not be able to write one; re-dispatching the gate is its only remedy."
        )
        assert perms.get("actions") == "write", f"recovery job {key!r} needs actions:write to re-dispatch the gate run"


def test_publishers_for_one_head_do_not_cancel_each_other(
    publish_doc: dict[str, object],
) -> None:
    """INV-2k. Two publishers for one head must not share a concurrency group.

    With `cancel-in-progress: false` a group is a one-deep queue: a run
    executing plus a run pending means a third arrival cancels the pending
    one. A head SHA routinely collects several gate runs and therefore
    several publishers, so keying the group on the head SHA alone let the
    queue drop publishers - including, in the worst case, the only one whose
    currency check would have said `publish`.

    Serialising them stopped being worth anything once the currency check
    landed: at most one publisher per head decides to write, so there is no
    upsert race left to serialise against.
    """
    conc = publish_doc.get("concurrency")
    assert isinstance(conc, dict), "the publisher must declare a concurrency block"
    group = str(conc.get("group", ""))
    assert "workflow_run.id" in group, (
        f"the publisher's concurrency group is {group!r}, which several publishers for one head all share. "
        "A shared group with cancel-in-progress: false cancels pending members, so a publisher that would "
        "have written the required context can be dropped before it runs. Key the group on the triggering "
        "run id as well."
    )


def test_every_gate_run_that_needs_the_hop_is_claimed_by_exactly_one_job(
    publish_doc: dict[str, object],
) -> None:
    """INV-2i. No gate run may fall between the publisher's jobs (#3313).

    The publisher's job conditions and its currency check are two halves of
    one rule, and they were not complementary. The currency check silences
    every publisher that is not the newest one for the head; the job
    conditions then declined to run for the newest one in two cases, and a
    head with no writer left never gets the required context at all:

      * the newest gate run concluded ``cancelled`` - the job condition
        dropped it, and every older publisher had already stood down as
        stale (PR #3287: the first-contributor approval released three
        parked runs at 12:00:02Z, concurrency cancelled run 30622786412
        three seconds later, and the survivor 30622733829 carried the
        *lower* id, so it read itself as stale);
      * the newest gate run came from ``pull_request_review`` - the job
        condition only admitted ``pull_request`` (PR #3293 pre-cycle: run
        30655244121 passed at 18:28:48Z, its in-job publish 403'd as every
        fork's does, and no publisher ever ran for it).

    Both heads sat at BLOCKED with nothing on the page to point at until
    the pull request was closed and reopened. So this is the invariant:
    for every gate run that needs the `workflow_run` hop, exactly one job
    in the publisher claims it - one to write the verdict, one to recover
    when there is no verdict to write.
    """
    jobs = _jobs(publish_doc)
    assert jobs, "the publisher must define jobs"

    for event, conclusion in itertools.product(GATE_EVENTS_NEEDING_THE_HOP, RUN_CONCLUSIONS):
        context = {
            "github.event.workflow_run.event": event,
            "github.event.workflow_run.conclusion": conclusion,
        }
        claimants = [key for key, job in jobs.items() if _eval_condition(str(job.get("if", "true")), context)]
        assert len(claimants) == 1, (
            f"a gate run with event={event!r} and conclusion={conclusion!r} is claimed by {claimants!r}. "
            "Exactly one publisher job must own it: zero leaves the head with no `review-bot-ack` context and "
            "no way to get one short of closing and reopening the pull request (#3313); two race on the "
            "check-run upsert."
        )

    # A merge_group gate run publishes its own context in-repo, where the
    # token is already writable. The hop must not run for it.
    for conclusion in RUN_CONCLUSIONS:
        context = {
            "github.event.workflow_run.event": "merge_group",
            "github.event.workflow_run.conclusion": conclusion,
        }
        claimants = [key for key, job in jobs.items() if _eval_condition(str(job.get("if", "true")), context)]
        assert not claimants, (
            f"jobs {claimants!r} run for a merge_group gate run (conclusion={conclusion!r}). Queue-side runs "
            "already publish the context themselves; a second writer only races the first."
        )


def test_publisher_stands_down_when_a_newer_gate_run_exists(
    publish_doc: dict[str, object],
) -> None:
    """INV-2h. The last writer must check that it is still the current one.

    Dropping cancelled runs closes the observed case but not the general one.
    Publishers wait for runners, so the order they write the check-run in is
    not the order the gate runs concluded in: a gate that finished first can
    have its publisher scheduled last and overwrite a newer, better answer.
    Since the check-run is upserted per head SHA, last writer wins outright.

    So the publisher resolves the newest gate run for its own head SHA and
    stays quiet if that is not itself, which makes the outcome independent of
    scheduling order rather than merely less likely to be wrong.
    """
    for key, job in _jobs(publish_doc).items():
        steps = _steps(job)
        script = "\n".join(str(step.get("run", "")) for step in steps)
        if PUBLISHER not in script:
            continue

        currency = [step for step in steps if step.get("id") == "currency"]
        assert currency, (
            f"publisher job {key!r} has no currency check; a stale publisher can "
            "overwrite a newer verdict because it happened to be scheduled later"
        )

        probe = str(currency[0].get("run", ""))
        assert "head_sha=" in probe, "the currency check must ask the API which gate runs exist for this head SHA"
        assert CURRENCY in probe, (
            f"publisher job {key!r} decides currency in workflow shell. The rule is what #3313 got wrong - "
            f"'newest id' is not 'newest verdict' - so it lives in {CURRENCY}, where the recorded incident "
            "histories are replayed against it in this file."
        )

        publishing = [
            step
            for step in steps
            if PUBLISHER in str(step.get("run", "")) or "Resolve the conclusion" in str(step.get("name", ""))
        ]
        assert publishing, f"expected publishing steps in job {key!r}"
        for step in publishing:
            guard = " ".join(str(step.get("if", "")).split())
            assert "currency.outputs.decision == 'publish'" in guard, (
                f"step {step.get('name')!r} in job {key!r} writes the verdict without "
                f"consulting the currency check; its condition is {guard!r}"
            )


def _currency_module() -> object:
    sys.path.insert(0, str(Path("scripts").resolve()))
    import ack_publisher_currency

    return ack_publisher_currency


def _gate_run(run_id: int, event: str, conclusion: str | None, attempt: int = 1) -> dict[str, object]:
    return {
        "id": run_id,
        "name": "Review-bot acknowledgement gate",
        "event": event,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "run_attempt": attempt,
    }


def test_currency_replays_pr_3287_where_the_survivor_had_the_lower_id() -> None:
    """The #3313 incident that proves id order is not verdict order.

    Approving PR #3287's first-time contributor released three parked gate
    runs at 12:00:02Z. Concurrency cancelled two of them within four
    seconds, and the survivor - 30622733829, which passed at 12:05:31Z -
    carried a LOWER id than the cancelled 30622786412.

    Under the old "stand down if a higher id exists" rule the survivor read
    itself as stale, the cancelled run's publisher never started, and the
    head carried no `review-bot-ack` context for six hours until the pull
    request was cycled. A run that will never publish is not a reason for
    the run that can to stay quiet.
    """
    module = _currency_module()
    runs = [
        _gate_run(30622722192, "pull_request", "success"),
        _gate_run(30622728511, "pull_request_review", "cancelled"),
        _gate_run(30622733829, "pull_request", "success"),
        _gate_run(30622786412, "pull_request", "cancelled"),
    ]
    assert module.decide(runs, 30622733829) == "publish"  # type: ignore[attr-defined]
    assert module.decide(runs, 30622722192) == "stand-down"  # type: ignore[attr-defined]


def test_currency_replays_pr_3293_where_the_newest_run_was_a_review_event() -> None:
    """The other #3313 shape: the newest gate run was `pull_request_review`.

    On PR #3293's pre-cycle head a048348, run 30655244121 passed at
    18:28:48Z. Its in-job publish 403'd, as every fork's does, and the
    publisher's job condition admitted only `pull_request`, so no publisher
    ran for it. The two older `pull_request` runs had already stood down as
    stale. That run is the head's current verdict and must publish.
    """
    module = _currency_module()
    runs = [
        _gate_run(30655070335, "pull_request", "success"),
        _gate_run(30655134036, "pull_request", "success"),
        _gate_run(30655244121, "pull_request_review", "success"),
    ]
    assert module.decide(runs, 30655244121) == "publish"  # type: ignore[attr-defined]
    assert module.decide(runs, 30655134036) == "stand-down"  # type: ignore[attr-defined]


def test_currency_redispatches_once_per_head_when_nothing_can_publish() -> None:
    """When every gate run on a head is cancelled, someone must re-dispatch.

    And exactly once. The mark of a re-dispatch is `run_attempt > 1` on some
    gate run for the head, which is GitHub's own state rather than a ledger
    this workflow would have to keep; an operator's manual re-run counts the
    same way, because a manual re-run IS the re-dispatch.
    """
    module = _currency_module()
    cancelled = [
        _gate_run(11, "pull_request", "cancelled"),
        _gate_run(12, "pull_request_review", "cancelled"),
    ]
    assert module.decide(cancelled, 12) == "redispatch"  # type: ignore[attr-defined]
    assert module.decide(cancelled, 11) == "stand-down"  # type: ignore[attr-defined]

    already_present = module.decide(cancelled, 12, context_present=True)  # type: ignore[attr-defined]
    assert already_present == "stand-down", "a head that already carries the context must not be churned"

    retried = [
        _gate_run(11, "pull_request", "cancelled"),
        _gate_run(12, "pull_request_review", "cancelled", attempt=2),
    ]
    assert module.decide(retried, 12) == "stand-down", "one re-dispatch per head SHA; this is the loop guard"  # type: ignore[attr-defined]


def test_currency_defers_to_a_successor_that_is_still_running() -> None:
    """A run with no conclusion yet may still produce a verdict.

    Overtaking it would put the older answer on the head and lose the newer
    one, which is the race the currency check exists to prevent.
    """
    module = _currency_module()
    runs = [
        _gate_run(21, "pull_request", "success"),
        _gate_run(22, "pull_request", None),
    ]
    assert module.decide(runs, 21) == "stand-down"  # type: ignore[attr-defined]


def test_every_gate_run_history_leaves_the_head_a_writer() -> None:
    """The guarantee #3313 asks for, stated as a property.

    For any history of gate runs on a head, at least one of them must reach
    a decision that eventually puts a conclusion on the commit: either it
    publishes, or it re-dispatches the gate so that a run which can publish
    exists. `stand-down` for every run is the bug - that is a head nobody
    will ever write, and the only recorded remedy was closing and reopening
    the pull request.
    """
    module = _currency_module()
    events = ("pull_request", "pull_request_review")
    conclusions = ("success", "failure", "cancelled")

    for size in (1, 2, 3):
        for combo in itertools.product(itertools.product(events, conclusions), repeat=size):
            runs = [_gate_run(100 + index, event, conclusion) for index, (event, conclusion) in enumerate(combo)]
            decisions = {run["id"]: module.decide(runs, int(run["id"])) for run in runs}  # type: ignore[attr-defined]
            writers = [decision for decision in decisions.values() if decision != "stand-down"]
            assert writers, (
                f"gate run history {combo!r} leaves every publisher standing down, so the head never gets a "
                f"review-bot-ack conclusion at all: {decisions!r}"
            )
            assert len(writers) == 1, (
                f"gate run history {combo!r} elects {len(writers)} writers: {decisions!r}. Two publishers race "
                "on the check-run upsert; two re-dispatches double-run the gate for one head."
            )


def test_currency_cli_reports_its_decision_to_github_output(tmp_path: Path) -> None:
    """The workflow reads the decision from `GITHUB_OUTPUT`, so the CLI has
    to put it there and not only on stdout."""
    module = _currency_module()
    payload = {"workflow_runs": [_gate_run(31, "pull_request_review", "success")]}
    runs_file = tmp_path / "runs.json"
    runs_file.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "github_output"

    import os

    previous = os.environ.get("GITHUB_OUTPUT")
    os.environ["GITHUB_OUTPUT"] = str(output)
    try:
        code = module.main(["--runs", str(runs_file), "--this-run", "31"])  # type: ignore[attr-defined]
    finally:
        if previous is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = previous

    assert code == 0
    assert "decision=publish" in output.read_text(encoding="utf-8")


def test_currency_ignores_runs_of_other_workflows() -> None:
    """The head SHA query returns every workflow's runs, and their ids sit
    in the same space as the gate's. Counting one as a successor would
    silence the publisher for a run that has nothing to do with the gate."""
    module = _currency_module()
    payload = {
        "workflow_runs": [
            _gate_run(41, "pull_request", "success"),
            {"id": 42, "name": "CI gate", "event": "pull_request", "status": "completed", "conclusion": "success"},
        ]
    }
    runs = module.gate_runs(payload)  # type: ignore[attr-defined]
    assert [run["id"] for run in runs] == [41]
    assert module.decide(runs, 41) == "publish"  # type: ignore[attr-defined]
