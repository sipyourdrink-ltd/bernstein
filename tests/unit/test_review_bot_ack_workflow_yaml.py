"""Structural assertions on the review-bot acknowledgement workflows
and the gate/sweeper scripts.

These tests pin the contract that:
    * the pre-merge gate emits a check named ``review-bot-ack`` on
      every PR event,
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
SWEEP_WF = Path(".github/workflows/review-bot-sweep.yml")
GATE_SCRIPT = Path("scripts/review_bot_ack.py")
SWEEP_SCRIPT = Path("scripts/review_bot_sweep.py")


@pytest.fixture(scope="module")
def gate_doc() -> dict[str, object]:
    return yaml.safe_load(GATE_WF.read_text(encoding="utf-8"))


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


def test_gate_triggers(gate_doc: dict[str, object]) -> None:
    on = _on(gate_doc)
    pr = on.get("pull_request")
    assert isinstance(pr, dict)
    types = pr.get("types") or []
    for t in ("opened", "synchronize", "edited"):
        assert t in types, f"gate must trigger on pull_request.{t}"
    assert "pull_request_review" in on


def test_gate_job_emits_review_bot_ack_check(
    gate_doc: dict[str, object],
) -> None:
    jobs = gate_doc.get("jobs")
    assert isinstance(jobs, dict)
    assert "review-bot-ack" in jobs, "gate workflow must define a job that produces the `review-bot-ack` check name"
    job = jobs["review-bot-ack"]
    assert isinstance(job, dict)
    assert job.get("name") == "review-bot-ack", (
        "job `name:` must equal the literal check name `review-bot-ack` (branch protection keys on the literal name)"
    )


def test_gate_concurrency_never_cancels_a_required_context(
    gate_doc: dict[str, object],
) -> None:
    """A cancelled run leaves a `cancelled` check-run that branch protection
    folds into the required context's verdict, blocking the PR even after a
    newer run concludes success (#3154, #3042). The group key must therefore
    separate runs born from different events, and cancel-in-progress must be
    off so a same-event duplicate reports a real conclusion instead of a
    cancelled tombstone.
    """
    conc = gate_doc.get("concurrency")
    assert isinstance(conc, dict), "workflow must declare a concurrency block"
    assert conc.get("cancel-in-progress") is False, (
        "cancel-in-progress must be false: a cancelled run of a required "
        "context poisons the rollup for its head SHA (#3154)"
    )
    group = str(conc.get("group", ""))
    assert "github.event_name" in group, (
        "the concurrency group must include the event name so runs born "
        "from different events never contend with each other (#3154)"
    )


def test_merge_group_job_verifies_instead_of_echoing(
    gate_doc: dict[str, object],
) -> None:
    """The merge_group emitter must not satisfy the required context
    unconditionally (#3114). It has to resolve the queued entry's pull
    request and require a successful PR-stage `review-bot-ack` check-run on
    that PR's head commit, failing closed on anything it cannot resolve.
    """
    jobs = gate_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get("merge-group-pass")
    assert isinstance(job, dict), "queue-side emitter job must exist"
    assert job.get("name") == "review-bot-ack"
    perms = job.get("permissions")
    assert isinstance(perms, dict) and perms.get("checks") == "read", (
        "the queue-side job needs checks:read to verify the PR-stage gate"
    )
    steps = job.get("steps") or []
    assert isinstance(steps, list) and steps, "job must have steps"
    scripts = "\n".join(str(s.get("run", "")) for s in steps if isinstance(s, dict))
    assert "check-runs" in scripts and "review-bot-ack" in scripts, (
        "the queue-side job must query check-runs for the PR-stage gate rather than passing unconditionally (#3114)"
    )
    assert "exit 1" in scripts, "the queue-side job must fail closed"
    assert "merge_group.head_ref" in (
        scripts + "\n".join(str(s.get("env") or {}) for s in steps if isinstance(s, dict))
    ), "the queued entry's PR must be resolved from the merge_group ref"


def test_gate_actions_sha_pinned() -> None:
    text = GATE_WF.read_text(encoding="utf-8")
    uses = [m.group(0) for m in re.finditer(r"uses:\s*[^\s#]+", text)]
    pat = re.compile(r"uses:\s*[\w./-]+@[0-9a-f]{40}\b")
    for line in uses:
        assert pat.match(line), f"action not SHA-pinned: {line}"


def test_gate_checkout_no_persist_credentials(gate_doc: dict[str, object]) -> None:
    jobs = gate_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs["review-bot-ack"]
    assert isinstance(job, dict)
    steps = job.get("steps") or []
    checkout = next(
        (s for s in steps if isinstance(s, dict) and "checkout" in str(s.get("uses", ""))),
        None,
    )
    assert checkout is not None
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
