"""Integration tests for the autofix daemon tick + label gate.

These exercise the full pipeline: the operator drops a PR with the
opt-in label and the trailer; one tick of the daemon dispatches an
attempt, writes the JSONL status log, and emits a clean audit chain.
Removing the label between ticks aborts the next attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bernstein.core.autofix.config import (
    DEFAULT_LABEL,
    AutofixConfig,
    RepoConfig,
)
from bernstein.core.autofix.daemon import (
    FailingCandidate,
    recent_attempts,
    tick_once,
)
from bernstein.core.autofix.dispatcher import (
    Dispatcher,
    DispatchResult,
)
from bernstein.core.autofix.gh_logs import LogExtraction
from bernstein.core.autofix.ownership import (
    PullRequestMetadata,
    decide_ownership,
)
from bernstein.core.security.audit import AuditLog

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Actions:
    comments: list[tuple[str, int, str]] = field(default_factory=list)
    labels: list[tuple[str, int, str]] = field(default_factory=list)
    removed: list[tuple[str, int, str]] = field(default_factory=list)

    def post_comment(self, repo: str, pr_number: int, body: str) -> None:
        self.comments.append((repo, pr_number, body))

    def add_label(self, repo: str, pr_number: int, label: str) -> None:
        self.labels.append((repo, pr_number, label))

    def remove_label(self, repo: str, pr_number: int, label: str) -> None:
        self.removed.append((repo, pr_number, label))


def _audit(tmp_path: Path) -> AuditLog:
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)
    return AuditLog(audit_dir=tmp_path / "audit", key_path=key_path)


def _config(repo_name: str = "owner/name") -> AutofixConfig:
    return AutofixConfig(
        poll_interval_seconds=1,
        log_byte_budget=4096,
        repos=(
            RepoConfig(
                name=repo_name,
                cost_cap_usd=10.0,
                label=DEFAULT_LABEL,
                allow_force_push=False,
            ),
        ),
    )


def _pr_with_label(label: str = DEFAULT_LABEL) -> PullRequestMetadata:
    return PullRequestMetadata(
        repo="owner/name",
        number=142,
        title="feat: example",
        body="feat\n\nbernstein-session-id: sess123",
        labels=(label,),
        head_sha="abc123abc123abc1",
        head_branch="feat/x",
        head_repo_full_name="owner/name",
        is_fork=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_tick_dispatches_and_logs(tmp_path: Path) -> None:
    """One tick produces an attempt record, JSONL line, and audit entries."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    audit = _audit(tmp_path)

    captured_goals: list[str] = []

    def hook(
        *,
        goal: str,
        model: str,
        effort: str,
        repo: str,
        head_branch: str,
        allow_force_push: bool,
        cost_cap_usd: float,
    ) -> DispatchResult:
        del effort, head_branch, allow_force_push, cost_cap_usd, model, repo
        captured_goals.append(goal)
        return DispatchResult(success=True, commit_sha="newsha", cost_usd=0.05)

    dispatcher = Dispatcher(
        audit=audit,
        action_adapter=_Actions(),
        dispatch_hook=hook,
    )

    candidate = FailingCandidate(
        pr=_pr_with_label(),
        run_id="987",
        log=LogExtraction(ok=True, body="ruff complaints"),
        session_id="sess123",
    )

    def source(repo_config: RepoConfig) -> list[FailingCandidate]:
        del repo_config
        return [candidate]

    records = tick_once(
        config=_config(),
        dispatcher=dispatcher,
        failing_source=source,
        workdir=workdir,
    )

    assert len(records) == 1
    assert records[0].outcome == "success"
    assert records[0].classification is not None
    assert records[0].classification.kind == "config"

    statuses = recent_attempts(workdir, limit=10)
    assert statuses, "status JSONL must be written"
    assert statuses[0]["repo"] == "owner/name"

    valid, errors = audit.verify()
    assert valid is True, errors

    assert "ruff complaints" in captured_goals[0]


def test_label_gate_blocks_dispatch(tmp_path: Path) -> None:
    """A PR whose label was removed between ticks is skipped at ownership."""
    decision = decide_ownership(
        _pr_with_label(label="not-the-right-one"),
        expected_label=DEFAULT_LABEL,
        session_lookup=lambda _sid: True,
    )
    assert decision.eligible is False


def test_unknown_session_blocks_dispatch(tmp_path: Path) -> None:
    """An unknown session id is rejected even when the label is set."""
    decision = decide_ownership(
        _pr_with_label(),
        expected_label=DEFAULT_LABEL,
        session_lookup=lambda _sid: False,
    )
    assert decision.eligible is False


def test_audit_chain_survives_multiple_ticks(tmp_path: Path) -> None:
    """Two ticks back-to-back leave the HMAC chain intact."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    audit = _audit(tmp_path)

    def hook(**kwargs: object) -> DispatchResult:
        del kwargs
        return DispatchResult(success=True, commit_sha="x", cost_usd=0.01)

    dispatcher = Dispatcher(
        audit=audit,
        action_adapter=_Actions(),
        dispatch_hook=hook,  # type: ignore[arg-type]
    )

    def source(repo_config: RepoConfig) -> list[FailingCandidate]:
        del repo_config
        return [
            FailingCandidate(
                pr=_pr_with_label(),
                run_id="1",
                log=LogExtraction(ok=True, body="ruff"),
                session_id="sess123",
            ),
        ]

    cfg = _config()
    tick_once(config=cfg, dispatcher=dispatcher, failing_source=source, workdir=workdir)
    tick_once(config=cfg, dispatcher=dispatcher, failing_source=source, workdir=workdir)

    valid, errors = audit.verify()
    assert valid is True, errors
