"""Tests for :mod:`bernstein.core.review_responder.responder`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.review_responder.dedup import DedupQueue
from bernstein.core.review_responder.gh_client import GhClient
from bernstein.core.review_responder.models import (
    ResponderConfig,
    ReviewComment,
    ReviewRound,
    RoundOutcome,
)
from bernstein.core.review_responder.responder import (
    GateAdvice,
    ReviewResponder,
    RunnerOutcome,
    build_always_allow_gate,
)
from bernstein.core.security.always_allow import AlwaysAllowEngine, AlwaysAllowRule
from bernstein.core.security.audit import AuditLog
from tests.unit.review_responder.conftest import FakeGhRunner, make_round


def _build_responder(
    *,
    tmp_path: Path,
    runner_outcome: RunnerOutcome,
    config: ResponderConfig | None = None,
    fake_gh: FakeGhRunner | None = None,
    gate_consult: Any = None,
    diff_provider: Any = None,
) -> tuple[ReviewResponder, FakeGhRunner, AuditLog, DedupQueue]:
    """Construct a fully wired responder with fakes for tests."""
    cfg = config or ResponderConfig(repo="o/r", quiet_window_s=0.5, per_round_cost_cap_usd=1.0)
    audit = AuditLog(tmp_path / "audit", key=b"k")
    queue = DedupQueue(state_path=tmp_path / "dedup.json")
    gh_runner = fake_gh or FakeGhRunner({"pulls/comments": (404, "")})
    gh_client = GhClient(runner=gh_runner)
    responder = ReviewResponder(
        config=cfg,
        runner=lambda r, p: runner_outcome,
        audit=audit,
        dedup=queue,
        gh=gh_client,
        gate_consult=gate_consult or (lambda _r, _o: GateAdvice(allowed=True, reason="ok")),
        diff_provider=diff_provider or (lambda _r: None),
    )
    return responder, gh_runner, audit, queue


def test_committed_round_records_audit_and_metric(tmp_path: Path, sample_comment: ReviewComment) -> None:
    """A successful runner produces a COMMITTED outcome and one audit row."""
    runner_outcome = RunnerOutcome(
        commit_sha="deadbeefcafe1234567890",
        cost_usd=0.10,
        summary="renamed foo→bar",
    )
    responder, fake_gh, audit, queue = _build_responder(tmp_path=tmp_path, runner_outcome=runner_outcome)
    queue.offer(sample_comment)
    round_obj = make_round(sample_comment)

    result = responder.run_round(round_obj)

    assert result.outcome is RoundOutcome.COMMITTED
    assert result.commit_sha == "deadbeefcafe1234567890"
    assert sample_comment.comment_id in result.addressed
    events = audit.query(event_type="review_responder.round")
    assert len(events) == 1
    details = events[0].details
    assert details["outcome"] == "committed"
    assert details["commit_sha"] == runner_outcome.commit_sha
    assert sample_comment.comment_id in details["comments"]
    # Summary comment posted to the PR issue thread.
    assert fake_gh.call_args_for(f"issues/{sample_comment.pr_number}/comments")
    # dedup record marks the comment with the final outcome.
    rec = queue.known(sample_comment.comment_id)
    assert rec is not None and rec.outcome == "committed"


def test_cost_cap_breach_triggers_needs_human(tmp_path: Path, sample_comment: ReviewComment) -> None:
    """A runner that overspends the cap produces a NEEDS_HUMAN outcome."""
    runner_outcome = RunnerOutcome(
        commit_sha="x",
        cost_usd=99.0,
        summary="ran hot",
    )
    cfg = ResponderConfig(repo="o/r", quiet_window_s=0.5, per_round_cost_cap_usd=0.50)
    responder, fake_gh, _audit, _queue = _build_responder(tmp_path=tmp_path, runner_outcome=runner_outcome, config=cfg)

    result = responder.run_round(make_round(sample_comment))

    assert result.outcome is RoundOutcome.COST_CAP_BREACHED
    assert result.cost_usd == pytest.approx(99.0)
    # Needs-human notice posted (no commit summary).
    summaries = fake_gh.call_args_for("issues/")
    assert any("escalating round" in (stdin or "") for _args, stdin in summaries)


def test_gate_denial_blocks_commit(tmp_path: Path, sample_comment: ReviewComment) -> None:
    """A blocking gate decision aborts the commit and posts needs-human."""
    runner_outcome = RunnerOutcome(commit_sha="x" * 40, cost_usd=0.05, summary="ok")
    responder, _gh, audit, _q = _build_responder(
        tmp_path=tmp_path,
        runner_outcome=runner_outcome,
        gate_consult=lambda _r, _o: GateAdvice(allowed=False, reason="not allow-listed"),
    )
    result = responder.run_round(make_round(sample_comment))
    assert result.outcome is RoundOutcome.NEEDS_HUMAN
    events = audit.query(event_type="review_responder.round")
    assert events[0].details["outcome"] == "needs_human"


def test_question_comments_are_dismissed_with_apology(tmp_path: Path, question_comment: ReviewComment) -> None:
    """Question-style comments never reach the runner."""
    invocations: list[Any] = []

    def runner(_r: ReviewRound, _p: str) -> RunnerOutcome:
        invocations.append(1)
        return RunnerOutcome(commit_sha="x", cost_usd=0.0, summary="x")

    cfg = ResponderConfig(repo="o/r")
    audit = AuditLog(tmp_path / "audit", key=b"k")
    queue = DedupQueue(state_path=tmp_path / "dedup.json")
    queue.offer(question_comment)
    responder = ReviewResponder(
        config=cfg,
        runner=runner,
        audit=audit,
        dedup=queue,
        gh=GhClient(runner=FakeGhRunner()),
        diff_provider=lambda _r: None,
    )
    result = responder.run_round(make_round(question_comment))
    assert invocations == []
    assert result.outcome is RoundOutcome.DISMISSED_QUESTION
    rec = queue.known(question_comment.comment_id)
    assert rec is not None and rec.outcome == "dismissed_question"


def test_stale_line_dismissed_when_diff_lacks_range(tmp_path: Path, stale_comment: ReviewComment) -> None:
    """A comment whose line is missing from the diff is dismissed as stale."""
    cfg = ResponderConfig(repo="o/r")
    audit = AuditLog(tmp_path / "audit", key=b"k")
    queue = DedupQueue(state_path=tmp_path / "dedup.json")
    queue.offer(stale_comment)
    runner_calls: list[Any] = []
    responder = ReviewResponder(
        config=cfg,
        runner=lambda r, p: runner_calls.append(1) or RunnerOutcome("x", 0.0, "x"),
        audit=audit,
        dedup=queue,
        gh=GhClient(runner=FakeGhRunner()),
        diff_provider=lambda _r: {"src/util.py": {1, 2, 3}},
    )
    result = responder.run_round(make_round(stale_comment))
    assert runner_calls == []
    assert result.outcome is RoundOutcome.DISMISSED_STALE
    assert (
        stale_comment.comment_id,
        "cited line range no longer present in PR diff",
    ) in result.dismissed


def test_audit_chain_replay_after_round(tmp_path: Path, sample_comment: ReviewComment) -> None:
    """A second AuditLog instance reads the round's HMAC chain unbroken."""
    runner_outcome = RunnerOutcome(commit_sha="abc", cost_usd=0.1, summary="ok")
    responder, _gh, _audit, _q = _build_responder(tmp_path=tmp_path, runner_outcome=runner_outcome)
    responder.run_round(make_round(sample_comment))

    replay = AuditLog(tmp_path / "audit", key=b"k")
    ok, errors = replay.verify()
    assert ok, errors
    events = replay.query(event_type="review_responder.round")
    assert len(events) == 1
    # Ensure the audit row carries reviewer + comment metadata.
    details = events[0].details
    assert "alice" in details["reviewers"]
    assert details["adapter"] == "claude"


def test_always_allow_gate_allows_when_rule_matches(
    sample_comment: ReviewComment,
) -> None:
    """The shipped gate allows a commit when the always-allow engine matches."""
    engine = AlwaysAllowEngine(
        rules=[
            AlwaysAllowRule(
                id="rr-allow-utils",
                tool="review_responder.commit",
                input_pattern="src/util.py",
                input_field="path",
                description="Allow responder commits to util.py",
            )
        ]
    )
    gate = build_always_allow_gate(engine)
    advice = gate(make_round(sample_comment), RunnerOutcome("x", 0.0, "ok"))
    assert advice.allowed is True


def test_always_allow_gate_denies_without_rule(sample_comment: ReviewComment) -> None:
    """Without a matching rule the gate denies — caller must opt-in explicitly."""
    gate = build_always_allow_gate(AlwaysAllowEngine(rules=[]))
    advice = gate(make_round(sample_comment), RunnerOutcome("x", 0.0, "ok"))
    assert advice.allowed is False
