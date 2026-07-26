"""A review bot that never ran must not be reported as a clean zero.

``scripts/review_bot_ack.py`` backs one of the two required contexts on
``main``, and its sticky summary is the artefact a human reads to decide
whether the bot findings were dealt with. It counted findings and nothing
else, so a bot that was rate limited and produced no review at all
contributed zero findings and the summary read::

    - Must-address findings: 0 (0 acknowledged, 0 open)

which is the same sentence a fully reviewed, genuinely clean PR gets.

These tests pin the per-bot run detection: a review anchored to the current
head commit is the only clean result, and a rate-limit notice, a review of an
older head, or no artefact at all are each reported as their own state.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "review_bot_ack.py"

HEAD = "1932e7b4b3200f7f5e15b3e17f39323a751e8da8"
OLDER = "67e724044f1beb34f2b571ec359152f0f937e4f7"

CODERABBIT = "coderabbitai[bot]"
SOURCERY = "sourcery-ai[bot]"

# Verbatim shapes observed on sipyourdrink-ltd/bernstein#3041 and #3165.
SOURCERY_RATE_LIMIT = "Sorry @chernistry, you have reached your weekly rate limit of 500000 diff characters.\n"
SOURCERY_CLEAN_REVIEW = "Hey - I've reviewed your changes and they look great!\n"
CODERABBIT_RATE_LIMIT = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n"
    "> [!WARNING]\n> ## Review limit reached\n"
)
CODERABBIT_CLEAN_REVIEW = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "No actionable comments were generated in the recent review.\n"
    f"Reviewing files that changed from the base of the PR and between {OLDER} and {HEAD}.\n"
)


@pytest.fixture
def ack() -> Generator[ModuleType, None, None]:
    """Load scripts/review_bot_ack.py as an importable module."""
    spec = importlib.util.spec_from_file_location("review_bot_ack_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _artifact(ack: ModuleType, author: str, body: str, commit_id: str | None = None, kind: str = "review"):
    return ack.BotArtifact(author=author, body=body, commit_id=commit_id, kind=kind)


# --- per-bot run detection -------------------------------------------------


def test_review_on_head_sha_is_a_clean_result(ack: ModuleType) -> None:
    """A real review anchored to the head commit is the only clean state."""
    artifacts = [_artifact(ack, SOURCERY, SOURCERY_CLEAN_REVIEW, commit_id=HEAD)]
    status = ack.classify_bot_run(SOURCERY, artifacts, HEAD)
    assert status.status == ack.BOT_REVIEWED
    assert status.clean is True


def test_summary_naming_the_head_sha_counts_as_reviewed(ack: ModuleType) -> None:
    """An issue comment with no ``commit_id`` still anchors via its body.

    CodeRabbit posts its review as a top-level comment, which carries no
    ``commit_id`` field, but names the reviewed range in the body.
    """
    artifacts = [_artifact(ack, CODERABBIT, CODERABBIT_CLEAN_REVIEW, kind="issue-comment")]
    status = ack.classify_bot_run(CODERABBIT, artifacts, HEAD)
    assert status.status == ack.BOT_REVIEWED


def test_rate_limited_sourcery_review_is_not_a_review(ack: ModuleType) -> None:
    """A rate-limit notice submitted as a review does not count as reviewed."""
    artifacts = [_artifact(ack, SOURCERY, SOURCERY_RATE_LIMIT, commit_id=HEAD)]
    status = ack.classify_bot_run(SOURCERY, artifacts, HEAD)
    assert status.status == ack.BOT_DECLINED
    assert status.clean is False


def test_rate_limited_coderabbit_comment_is_not_a_review(ack: ModuleType) -> None:
    """CodeRabbit's review-limit warning does not count as reviewed."""
    artifacts = [_artifact(ack, CODERABBIT, CODERABBIT_RATE_LIMIT, kind="issue-comment")]
    status = ack.classify_bot_run(CODERABBIT, artifacts, HEAD)
    assert status.status == ack.BOT_DECLINED


def test_review_of_an_older_head_is_stale(ack: ModuleType) -> None:
    """A review that covers only a superseded commit is not a clean result."""
    artifacts = [_artifact(ack, SOURCERY, SOURCERY_CLEAN_REVIEW, commit_id=OLDER)]
    status = ack.classify_bot_run(SOURCERY, artifacts, HEAD)
    assert status.status == ack.BOT_STALE
    assert status.clean is False


def test_no_artifact_at_all_is_absent(ack: ModuleType) -> None:
    """A configured bot with nothing on the PR is reported as absent."""
    status = ack.classify_bot_run(CODERABBIT, [], HEAD)
    assert status.status == ack.BOT_ABSENT
    assert status.clean is False


def test_only_the_named_bot_is_considered(ack: ModuleType) -> None:
    """One bot's review never stands in for another bot's."""
    artifacts = [_artifact(ack, SOURCERY, SOURCERY_CLEAN_REVIEW, commit_id=HEAD)]
    assert ack.classify_bot_run(CODERABBIT, artifacts, HEAD).status == ack.BOT_ABSENT


def test_pr_3041_shape_reports_both_bots_as_not_run(ack: ModuleType) -> None:
    """The exact artefact set from #3041: neither bot produced a review."""
    artifacts = [
        _artifact(ack, SOURCERY, SOURCERY_RATE_LIMIT, commit_id=OLDER),
        _artifact(ack, CODERABBIT, CODERABBIT_RATE_LIMIT, kind="issue-comment"),
    ]
    statuses = ack.classify_review_coverage(artifacts, HEAD)
    assert {s.login: s.clean for s in statuses} == {SOURCERY: False, CODERABBIT: False}


def test_bot_artifacts_are_built_from_the_fetched_comment_sources(ack: ModuleType) -> None:
    """Every fetched source contributes artefacts, tagged with its kind."""
    sources = {
        "review": [{"user": {"login": SOURCERY}, "body": SOURCERY_CLEAN_REVIEW, "commit_id": HEAD}],
        "review-comment": [{"user": {"login": CODERABBIT}, "body": "nit: spacing", "commit_id": HEAD}],
        "issue-comment": [
            {"user": {"login": CODERABBIT}, "body": CODERABBIT_CLEAN_REVIEW},
            {"user": {"login": "chernistry"}, "body": "a human comment"},
        ],
    }
    artifacts = ack.bot_artifacts_from(sources)
    assert [(a.author, a.kind) for a in artifacts] == [
        (SOURCERY, "review"),
        (CODERABBIT, "review-comment"),
        (CODERABBIT, "issue-comment"),
    ]


def test_each_comment_endpoint_is_paginated_once(ack: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Findings and coverage share one fetch instead of walking twice.

    ``paginate`` follows up to 30 pages per endpoint and this gate runs on
    every pull request, so a second sweep of the same endpoints is real cost.
    """
    calls: list[str] = []
    monkeypatch.setattr(ack, "paginate", lambda url, token: calls.append(url) or [])
    ack.fetch_comment_sources("owner", "repo", 1, "token")
    assert len(calls) == len(set(calls)) == 3


# --- summary rendering -----------------------------------------------------


def _outcome(ack: ModuleType, statuses: list[object]):
    return ack.GateOutcome(head_sha=HEAD, bot_statuses=statuses)


def test_summary_flags_zero_findings_from_a_bot_that_never_ran(ack: ModuleType) -> None:
    """Zero findings plus a bot that did not run is not presented as clean."""
    artifacts = [
        _artifact(ack, SOURCERY, SOURCERY_RATE_LIMIT, commit_id=OLDER),
        _artifact(ack, CODERABBIT, CODERABBIT_RATE_LIMIT, kind="issue-comment"),
    ]
    outcome = _outcome(ack, ack.classify_review_coverage(artifacts, HEAD))

    summary = ack.render_summary(outcome)

    assert "Must-address findings: **0**" in summary
    assert "not a clean result" in summary
    assert SOURCERY in summary
    assert CODERABBIT in summary


def test_summary_reports_a_fully_reviewed_pr_as_clean(ack: ModuleType) -> None:
    """When every bot reviewed the head commit the caveat is absent."""
    artifacts = [
        _artifact(ack, SOURCERY, SOURCERY_CLEAN_REVIEW, commit_id=HEAD),
        _artifact(ack, CODERABBIT, CODERABBIT_CLEAN_REVIEW, kind="issue-comment"),
    ]
    outcome = _outcome(ack, ack.classify_review_coverage(artifacts, HEAD))

    summary = ack.render_summary(outcome)

    assert "not a clean result" not in summary
    assert "All must-address findings are resolved or acknowledged." in summary


def test_summary_lists_every_configured_bot(ack: ModuleType) -> None:
    """The coverage section enumerates the configured bots, not just seen ones."""
    outcome = _outcome(ack, ack.classify_review_coverage([], HEAD))
    summary = ack.render_summary(outcome)
    for login in ack.REVIEW_BOT_LOGINS:
        assert login in summary


# --- exit status -----------------------------------------------------------


def test_require_review_fails_when_a_bot_did_not_run(ack: ModuleType) -> None:
    """``--require-review`` turns an unreviewed bot into a gate failure."""
    artifacts = [_artifact(ack, SOURCERY, SOURCERY_RATE_LIMIT, commit_id=OLDER)]
    outcome = _outcome(ack, ack.classify_review_coverage(artifacts, HEAD))
    assert outcome.unreviewed_bots
    assert ack.exit_code(outcome, require_review=True) == 1


def test_default_exit_status_is_unchanged_by_an_unreviewed_bot(ack: ModuleType) -> None:
    """Without the flag the gate keeps failing only on open findings.

    ``review-bot-ack`` is a required context, so an upstream rate limit must
    not wedge every PR on the repository by default.
    """
    artifacts = [_artifact(ack, SOURCERY, SOURCERY_RATE_LIMIT, commit_id=OLDER)]
    outcome = _outcome(ack, ack.classify_review_coverage(artifacts, HEAD))
    assert ack.exit_code(outcome, require_review=False) == 0
