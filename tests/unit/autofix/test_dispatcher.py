"""Unit tests for the autofix dispatcher."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bernstein.core.autofix.classifier import classify_failure
from bernstein.core.autofix.config import (
    DEFAULT_LABEL,
    MAX_ATTEMPTS_PER_PUSH,
    RepoConfig,
)
from bernstein.core.autofix.dispatcher import (
    AttemptCounter,
    Dispatcher,
    DispatchResult,
    synthesise_goal,
)
from bernstein.core.autofix.gh_logs import LogExtraction
from bernstein.core.autofix.ownership import PullRequestMetadata
from bernstein.core.security.audit import AuditLog

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


@dataclass
class _Actions:
    """Recording action adapter so tests can assert on side-effects."""

    comments: list[tuple[str, int, str]] = field(default_factory=list)
    labels: list[tuple[str, int, str]] = field(default_factory=list)
    removed: list[tuple[str, int, str]] = field(default_factory=list)

    def post_comment(self, repo: str, pr_number: int, body: str) -> None:
        self.comments.append((repo, pr_number, body))

    def add_label(self, repo: str, pr_number: int, label: str) -> None:
        self.labels.append((repo, pr_number, label))

    def remove_label(self, repo: str, pr_number: int, label: str) -> None:
        self.removed.append((repo, pr_number, label))


@dataclass
class _DispatchSpy:
    """Stub dispatch hook that records its inputs and replays a result."""

    result: DispatchResult
    raises: Exception | None = None
    captured: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self,
        *,
        goal: str,
        model: str,
        effort: str,
        repo: str,
        head_branch: str,
        allow_force_push: bool,
        cost_cap_usd: float,
    ) -> DispatchResult:
        self.captured.append(
            {
                "goal": goal,
                "model": model,
                "effort": effort,
                "repo": repo,
                "head_branch": head_branch,
                "allow_force_push": allow_force_push,
                "cost_cap_usd": cost_cap_usd,
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.result


def _audit(tmp_path: Path) -> AuditLog:
    """Spin up an AuditLog with an isolated key file."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 32)
    key_path.chmod(0o600)
    return AuditLog(audit_dir=tmp_path / "audit", key_path=key_path)


def _pr(**overrides: object) -> PullRequestMetadata:
    base: dict[str, object] = {
        "repo": "owner/name",
        "number": 142,
        "title": "feat",
        "body": "feat\n\nbernstein-session-id: sess123",
        "labels": (DEFAULT_LABEL,),
        "head_sha": "deadbeefdeadbeef",
        "head_branch": "feat/example",
        "head_repo_full_name": "owner/name",
        "is_fork": False,
    }
    base.update(overrides)
    return PullRequestMetadata(**base)  # type: ignore[arg-type]


def _repo_config(**overrides: object) -> RepoConfig:
    base: dict[str, object] = {
        "name": "owner/name",
        "cost_cap_usd": 5.0,
        "label": DEFAULT_LABEL,
        "allow_force_push": False,
    }
    base.update(overrides)
    return RepoConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Classifier routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("log_body", "expected_model", "expected_kind"),
    [
        ("CodeQL: cve-2024-1111 vulnerability", "opus", "security"),
        ("test_x failed: timeout exceeded", "sonnet", "flaky"),
        ("ruff check found violations", "haiku", "config"),
    ],
)
def test_classifier_routing_picks_correct_model(
    tmp_path: Path,
    log_body: str,
    expected_model: str,
    expected_kind: str,
) -> None:
    """Each classifier bucket maps to its documented bandit arm."""
    spy = _DispatchSpy(result=DispatchResult(success=True, commit_sha="sha", cost_usd=0.1))
    dispatcher = Dispatcher(
        audit=_audit(tmp_path),
        action_adapter=_Actions(),
        dispatch_hook=spy,
    )
    record = dispatcher.dispatch(
        repo_config=_repo_config(),
        pr=_pr(),
        run_id="999",
        log=LogExtraction(ok=True, body=log_body),
        session_id="sess123",
    )
    assert record.outcome == "success"
    assert record.classification is not None
    assert record.classification.kind == expected_kind
    assert record.classification.model == expected_model
    assert spy.captured[0]["model"] == expected_model


# ---------------------------------------------------------------------------
# Attempt cap
# ---------------------------------------------------------------------------


def test_fourth_attempt_escalates_to_human(tmp_path: Path) -> None:
    """The fourth attempt on the same SHA flips to ``needs-human``."""
    counter = AttemptCounter()
    actions = _Actions()
    spy = _DispatchSpy(result=DispatchResult(success=True, cost_usd=0.0))
    dispatcher = Dispatcher(
        audit=_audit(tmp_path),
        action_adapter=actions,
        dispatch_hook=spy,
        attempt_counter=counter,
    )
    pr = _pr()
    cfg = _repo_config()
    log = LogExtraction(ok=True, body="config drift")

    for _ in range(MAX_ATTEMPTS_PER_PUSH):
        rec = dispatcher.dispatch(
            repo_config=cfg, pr=pr, run_id="r", log=log, session_id="sess123"
        )
        assert rec.outcome == "success"

    overflow = dispatcher.dispatch(
        repo_config=cfg, pr=pr, run_id="r", log=log, session_id="sess123"
    )
    assert overflow.outcome == "needs_human"
    assert any(label == "needs-human" for _, _, label in actions.labels)
    # No dispatch on the overflow attempt.
    assert len(spy.captured) == MAX_ATTEMPTS_PER_PUSH


# ---------------------------------------------------------------------------
# Cost cap
# ---------------------------------------------------------------------------


def test_cost_cap_aborts_and_comments(tmp_path: Path) -> None:
    """Spend over the per-repo cap aborts the attempt cleanly."""
    actions = _Actions()
    spy = _DispatchSpy(
        result=DispatchResult(success=True, commit_sha="sha", cost_usd=10.0)
    )
    dispatcher = Dispatcher(
        audit=_audit(tmp_path),
        action_adapter=actions,
        dispatch_hook=spy,
    )
    record = dispatcher.dispatch(
        repo_config=_repo_config(cost_cap_usd=1.0),
        pr=_pr(),
        run_id="123",
        log=LogExtraction(ok=True, body="ruff failed"),
        session_id="sess123",
    )
    assert record.outcome == "cost_capped"
    assert record.cost_usd == 10.0
    assert any("exceeded" in body for _, _, body in actions.comments)


def test_cost_cap_zero_means_unlimited(tmp_path: Path) -> None:
    """A cost cap of 0 USD means 'no cap' — matches CostTracker semantics."""
    spy = _DispatchSpy(result=DispatchResult(success=True, cost_usd=99.99))
    dispatcher = Dispatcher(
        audit=_audit(tmp_path),
        action_adapter=_Actions(),
        dispatch_hook=spy,
    )
    record = dispatcher.dispatch(
        repo_config=_repo_config(cost_cap_usd=0.0),
        pr=_pr(),
        run_id="42",
        log=LogExtraction(ok=True, body="config issue"),
        session_id="sess123",
    )
    assert record.outcome == "success"


# ---------------------------------------------------------------------------
# Audit chain
# ---------------------------------------------------------------------------


def test_audit_chain_is_intact_after_attempt(tmp_path: Path) -> None:
    """Each dispatch writes a start+finish event; the chain verifies clean."""
    audit = _audit(tmp_path)
    dispatcher = Dispatcher(
        audit=audit,
        action_adapter=_Actions(),
        dispatch_hook=_DispatchSpy(
            result=DispatchResult(success=True, commit_sha="sha", cost_usd=0.5)
        ),
    )
    dispatcher.dispatch(
        repo_config=_repo_config(),
        pr=_pr(),
        run_id="555",
        log=LogExtraction(ok=True, body="ruff: E501"),
        session_id="sess123",
    )
    valid, errors = audit.verify()
    assert valid is True, errors


def test_attempt_id_links_open_and_close_events(tmp_path: Path) -> None:
    """The two audit events for one attempt share an attempt_id."""
    audit_dir = tmp_path / "audit"
    audit = _audit(tmp_path)
    dispatcher = Dispatcher(
        audit=audit,
        action_adapter=_Actions(),
        dispatch_hook=_DispatchSpy(
            result=DispatchResult(success=True, commit_sha="sha", cost_usd=0.5)
        ),
    )
    dispatcher.dispatch(
        repo_config=_repo_config(),
        pr=_pr(),
        run_id="777",
        log=LogExtraction(ok=True, body="ruff complaint"),
        session_id="sess123",
    )

    log_files = sorted(audit_dir.glob("*.jsonl"))
    assert log_files, "audit log must exist"
    events = [
        json.loads(line)
        for line in log_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 2
    assert events[0]["event_type"] == "autofix.attempt.start"
    assert events[1]["event_type"] == "autofix.attempt.finish"
    assert events[0]["details"]["attempt_id"] == events[1]["details"]["attempt_id"]


# ---------------------------------------------------------------------------
# Hook errors
# ---------------------------------------------------------------------------


def test_hook_exception_marks_failed(tmp_path: Path) -> None:
    """A raising dispatch hook becomes a 'failed' outcome (not a crash)."""
    spy = _DispatchSpy(
        result=DispatchResult(success=False),
        raises=RuntimeError("network down"),
    )
    dispatcher = Dispatcher(
        audit=_audit(tmp_path),
        action_adapter=_Actions(),
        dispatch_hook=spy,
    )
    record = dispatcher.dispatch(
        repo_config=_repo_config(),
        pr=_pr(),
        run_id="11",
        log=LogExtraction(ok=True, body="config"),
        session_id="sess123",
    )
    assert record.outcome == "failed"
    assert "network down" in record.reason


# ---------------------------------------------------------------------------
# Goal synthesis
# ---------------------------------------------------------------------------


def test_synthesise_goal_includes_session_trailer() -> None:
    """The synthesised goal carries the trailer so downstream PRs chain it."""
    classification = classify_failure("ruff: E501 too long")
    goal = synthesise_goal(
        repo="owner/name",
        pr_number=42,
        run_id="r",
        classification=classification,
        log_excerpt="ruff: E501 too long",
        session_id="abc12345",
    )
    assert "owner/name#42" in goal
    assert "bernstein-session-id: abc12345" in goal


def test_force_push_flag_is_propagated(tmp_path: Path) -> None:
    """allow_force_push from the repo config reaches the dispatch hook."""
    spy = _DispatchSpy(result=DispatchResult(success=True, cost_usd=0.0))
    dispatcher = Dispatcher(
        audit=_audit(tmp_path),
        action_adapter=_Actions(),
        dispatch_hook=spy,
    )
    dispatcher.dispatch(
        repo_config=_repo_config(allow_force_push=True),
        pr=_pr(),
        run_id="r",
        log=LogExtraction(ok=True, body="config"),
        session_id="sess123",
    )
    assert spy.captured[0]["allow_force_push"] is True
