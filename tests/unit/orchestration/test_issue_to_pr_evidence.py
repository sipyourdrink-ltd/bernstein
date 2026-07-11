"""Evidence-bundle wiring in the issue->PR pipeline body (issue #2362, AC3).

``tick_pr_open`` links the sealed evidence bundle of the task the diff resolves,
so a PR opened by the pipeline carries the proof-of-done pointer. Fail-open: a
missing or unreadable bundle never blocks PR creation and leaves the body as the
plain "Resolves #N" text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.evidence.bundle import EvidenceProducer, run_evidence_gate
from bernstein.core.orchestration.issue_to_pr import (
    DiffProposal,
    IssueContext,
    IssuePRClient,
    IssueToPRConfig,
    IssueToPRPipeline,
    PlanProposal,
    Stages,
    Triggers,
)
from bernstein.github_app.evidence_projection import EVIDENCE_BUNDLE_MARKER
from tests.unit.orchestration.test_issue_to_pr import FakeRunner, _approved_sticky, _issue

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_KEY = b"k" * 32
_STICKY_ID = 90


def _seal_bundle(workdir: Path, task_id: str) -> None:
    """Seal a deterministic, verifiable evidence bundle for ``task_id``."""

    def runner(_p: EvidenceProducer) -> tuple[int, bytes]:
        return 0, b"7 passed\n"

    run_evidence_gate(
        workdir=workdir,
        task_id=task_id,
        producers=(EvidenceProducer(name="tests", kind="test", command=("run",), required=True),),
        runner=runner,
        timestamp=1,
        hmac_key=_KEY,
    )


def _pipeline_with_task(runner: FakeRunner, workdir: Path, task_id: str) -> IssueToPRPipeline:
    """Build a pipeline whose diff resolves ``task_id`` under ``workdir``."""

    def diff_gen(ctx: IssueContext, plan: PlanProposal) -> DiffProposal:
        return DiffProposal(
            patch="diff --git a/x b/x\n",
            branch=plan.branch,
            commit_message=f"feat: resolve #{ctx.number}",
            base="main",
            task_id=task_id,
        )

    return IssueToPRPipeline(
        config=IssueToPRConfig(
            triggers=Triggers(label_required="ai-welcome"),
            stages=Stages(plan_comment_required_approval=True),
        ),
        client=IssuePRClient(runner=runner),
        diff_generator=diff_gen,
        apply_diff=lambda _d: "deadbeef",
        workdir=workdir,
    )


def test_pr_open_links_bundle_when_task_sealed_one(tmp_path: Path) -> None:
    """The opened PR body carries the marker + verify command when a bundle exists."""
    _seal_bundle(tmp_path, "T-iss-1")
    runner = FakeRunner(issue=_issue(), comments=[_approved_sticky(_STICKY_ID)])
    pipe = _pipeline_with_task(runner, tmp_path, "T-iss-1")

    report = pipe.tick_pr_open("acme/web", 7)

    assert report.pr_number is not None
    body = runner.opened_prs[0]["body"]
    assert EVIDENCE_BUNDLE_MARKER in body
    assert "bernstein evidence verify T-iss-1" in body
    # The base "Resolves" text is preserved.
    assert "Resolves #7" in body


def test_pr_open_omits_evidence_when_no_bundle(tmp_path: Path) -> None:
    """With no sealed bundle the body is exactly the plain "Resolves #N" text."""
    runner = FakeRunner(issue=_issue(), comments=[_approved_sticky(_STICKY_ID)])
    pipe = _pipeline_with_task(runner, tmp_path, "T-absent")

    report = pipe.tick_pr_open("acme/web", 7)

    assert report.pr_number is not None
    body = runner.opened_prs[0]["body"]
    assert EVIDENCE_BUNDLE_MARKER not in body
    assert body == f"Resolves #7.\n\nPlan: see comment #{_STICKY_ID} on the issue.\n"


def test_pr_open_swallows_bundle_resolution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception while resolving the bundle is swallowed; the PR still opens."""

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("bundle store exploded")

    monkeypatch.setattr("bernstein.core.evidence.bundle.read_evidence_bundle", _boom)
    runner = FakeRunner(issue=_issue(), comments=[_approved_sticky(_STICKY_ID)])
    pipe = _pipeline_with_task(runner, tmp_path, "T-boom")

    report = pipe.tick_pr_open("acme/web", 7)

    # Fail-open: PR still opened, body unchanged, no marker.
    assert report.pr_number is not None
    body = runner.opened_prs[0]["body"]
    assert EVIDENCE_BUNDLE_MARKER not in body
    assert "Resolves #7" in body
