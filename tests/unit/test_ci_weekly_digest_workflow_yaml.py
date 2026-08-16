"""Structural hardening assertions for ``ci-weekly-digest.yml``.

The aggregation maths live in ``scripts/ci_weekly_digest.py`` (covered by
``test_ci_weekly_digest.py``). The *shell* step that collects signal and
mutates the tracker is equally trust-critical: a swallowed collection error
publishes a false "clean" digest while still closing tracking issues, a
default-capped cleanup query leaves stale issues open forever, a blind
create-fallback can open a duplicate digest, and a midnight-truncated window
widens the 7-day cutoff by up to a day. These tests pin those invariants on
the workflow text so a regression is caught at unit time, not in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


WORKFLOW = Path(".github/workflows/ci-weekly-digest.yml")
STEP_NAME = "Build and publish weekly digest"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in cast("dict[object, object]", value).items()}


@pytest.fixture(scope="module")
def workflow_text() -> str:
    """The raw workflow file text, including header comments."""
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def digest_run(workflow_text: str) -> str:
    """The shell body of the digest build-and-publish step."""
    parsed = cast("object", yaml.safe_load(workflow_text))
    workflow = _mapping(parsed)
    jobs = _mapping(workflow.get("jobs"))
    digest = _mapping(jobs.get("digest"))
    steps = digest.get("steps")
    assert isinstance(steps, list)
    for step in cast("list[object]", steps):
        step_map = _mapping(step)
        if step_map.get("name") == STEP_NAME:
            run = step_map.get("run")
            assert isinstance(run, str)
            return run
    raise AssertionError(f"step {STEP_NAME!r} not found")


def _slice(run: str, start: str, end: str) -> str:
    assert start in run, f"marker {start!r} missing"
    assert end in run, f"marker {end!r} missing"
    return run.split(start, 1)[1].split(end, 1)[0]


def _code_only(fragment: str) -> str:
    """Drop shell comment lines so assertions target executable text only."""
    return "\n".join(ln for ln in fragment.splitlines() if not ln.strip().startswith("#"))


# --- item 1: collection errors must fail the step, not publish a false clean ---


def test_collection_commands_do_not_swallow_errors(digest_run: str) -> None:
    collect = _slice(digest_run, "# ----- collect:", "# ----- classify")
    # both collection sources present ...
    assert "gh issue list" in collect
    assert "gh api --paginate" in collect
    # ... and neither executable line is guarded by `|| true`, so
    # transport/auth/pagination failures fail the step before any issue is
    # closed. (Comment prose may mention the phrase; only code is checked.)
    assert "|| true" not in _code_only(collect), "collection command still swallows errors with `|| true`"


# --- item 2: cleanup must not silently cap at gh's 30-item default -------------


def test_rollforward_cleanup_query_uses_explicit_limit(digest_run: str) -> None:
    roll_forward = _slice(digest_run, "# ----- roll forward:", "# ----- close per-event")
    assert "gh issue list" in roll_forward
    assert "--limit" in roll_forward, "prior-digest cleanup query omits --limit (caps at 30)"


def test_close_skipped_cleanup_query_uses_explicit_limit(digest_run: str) -> None:
    close_events = _slice(digest_run, "# ----- close per-event", "# ----- run summary")
    assert "gh issue list" in close_events
    assert "--limit" in close_events, "per-event cleanup query omits --limit (caps at 30)"


# --- item 3: issue creation must never blindly retry into a duplicate ----------


def test_issue_create_has_no_blind_fallback(digest_run: str) -> None:
    normalized = " ".join(digest_run.split())
    assert "|| gh issue create" not in normalized, "blind create-fallback can open a duplicate digest"


def test_issue_create_probes_label_existence(digest_run: str) -> None:
    # The `ci` label is applied only when confirmed to exist, so a missing
    # label degrades deterministically instead of via an error-triggered retry.
    assert "gh label list" in digest_run


# --- item 4: the window must use the exact 7-day cutoff, not midnight ----------


def test_digest_window_uses_exact_timestamp_cutoff(digest_run: str) -> None:
    # The full ISO timestamp (SINCE), not a midnight-truncated date, drives
    # both the run and the auto-release-skipped filters.
    assert "SINCE_DATE" not in digest_run, "digest window still truncates SINCE to a midnight date"
    assert "created=>=${SINCE}" in digest_run
    assert "updated:>=${SINCE}" in digest_run


# --- item 5: the alert computation must not be dead code (issue #3952) ---------
#
# The `digest` job has exactly one step. A GITHUB_ENV write in that step can
# never be read by anything -- there is no later step in this job, and
# GITHUB_ENV does not cross job boundaries. Computing an alert file and a
# has_signal flag just to hand them to GITHUB_ENV was therefore dead code:
# a week whose numbers crossed the alert threshold produced the same silent
# digest as a quiet one. Resolution: delete the computation rather than wire
# a second consumer -- a quarter of weekly digests (12 issues, 4 of them
# flagged "Chronically red -- assign an owner") produced zero follow-up
# action on the already-prominent in-body recommendation, so a second
# notification surfacing the same data was judged not worth the added
# maintenance surface.


def test_step_does_not_write_dead_github_env(digest_run: str) -> None:
    # Comment prose is allowed to explain *why* (it does); only code is
    # checked, same idiom as test_collection_commands_do_not_swallow_errors.
    code = _code_only(digest_run)
    assert "GITHUB_ENV" not in code, "digest step writes to GITHUB_ENV, but this job has no later step to read it"


def test_step_does_not_request_unread_alert_file(digest_run: str) -> None:
    code = _code_only(digest_run)
    assert "--alert-file" not in code, "digest step still asks the script to compute an alert file"
    assert "ALERT_FILE" not in code
    assert "HAS_SIGNAL" not in code


def test_header_does_not_advertise_a_firing_alert(workflow_text: str) -> None:
    # The header previously claimed "a threshold alert fires" -- untrue, since
    # nothing ever consumed the computed alert. The header must not restate
    # that claim once the dead computation is gone.
    assert "alert fires" not in workflow_text.lower(), "header still advertises a threshold alert nothing implements"
