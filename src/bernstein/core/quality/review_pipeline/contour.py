"""The fix-until-green review contour and its per-pass provenance.

``bernstein review --pipeline`` used to stop at a verdict, leaving the part
that turns a verdict into an outcome on the pull request to a shell loop
around the CLI.  Every such loop invented its own stop condition, its own
idea of "red", and its own (usually absent) provenance, so two operators
reviewing the same PR could not compare results.

This module owns that loop:

1. wait, bounded, for the PR's check rollup to settle;
2. run the review pipeline against the PR's current diff;
3. stop when the pipeline approves and (under ``until_checks_green``) the
   checks are green;
4. otherwise run a fix pass whose inputs are the verdict *and* the failing
   checks' log excerpts, then start the next pass;
5. stop at ``max_passes`` with an explicit ``needs-operator`` outcome -- never
   an approval -- and a non-zero exit code.

Every pass emits a review receipt binding the reviewed diff hash, the ruleset
digest, the pass index and the verdict, each one carrying the previous pass's
spine anchor.  The artefact the operator ends up with *is* the proof: strip
the chain and there is no record that the PR was reviewed at all, let alone
under which standard.

The loop takes its collaborators as callables so the CLI stays thin and the
stop condition is testable without a network: ``fetch_diff``, ``read_rollup``,
``review``, ``fetch_logs``, ``fix_runner`` and ``emit_receipt``.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from bernstein.core.quality.review_pipeline.ruleset import EMPTY_RULESET

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from bernstein.core.quality.review_pipeline.ruleset import ReviewRuleset
    from bernstein.core.quality.review_pipeline.runner import DiffSource
    from bernstein.core.quality.review_pipeline.schema import ReviewPipeline
    from bernstein.core.quality.review_pipeline.verdict import FinalVerdict, PipelineVerdict

logger = logging.getLogger(__name__)

#: State of a PR's aggregate check rollup.
CheckState = Literal["green", "red", "pending"]

#: How the contour ended.  ``needs-operator`` is never an approval.
ContourOutcome = Literal["approved", "needs-operator"]

#: Check conclusions that count as red.  Everything else (``SUCCESS``,
#: ``NEUTRAL``, ``SKIPPED``) leaves the rollup green.
_FAILING_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE", "ERROR"}
)

#: Statuses that mean the check has not produced a conclusion yet.
_PENDING_STATUSES = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"})

#: Bytes of failing-check log handed to one fix pass, per check.
DEFAULT_LOG_BYTE_BUDGET = 16_000

_RUN_ID_RE = re.compile(r"/runs/(\d+)")


# ---------------------------------------------------------------------------
# Check rollup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckRun:
    """One entry of a PR's check rollup.

    Attributes:
        name: Check name as GitHub reports it.
        status: Lifecycle status (``QUEUED`` / ``IN_PROGRESS`` / ``COMPLETED``).
        conclusion: Terminal conclusion (``SUCCESS`` / ``FAILURE`` / ...).
        run_id: Workflow run id parsed out of the details url, used to fetch
            the failing log; empty when the entry is not an Actions run.
        url: The check's details url.
    """

    name: str
    status: str = ""
    conclusion: str = ""
    run_id: str = ""
    url: str = ""

    @property
    def is_pending(self) -> bool:
        """True while the check has not reached a conclusion."""
        return not self.conclusion and self.status.upper() != "COMPLETED"

    @property
    def is_failing(self) -> bool:
        """True when the check reached a conclusion the merge gate rejects."""
        return self.conclusion.upper() in _FAILING_CONCLUSIONS


@dataclass(frozen=True)
class CheckRollup:
    """A PR's aggregate check state.

    Attributes:
        state: ``pending`` while any check is unfinished, then ``red`` if any
            check failed, else ``green``.  A PR with no checks is green.
        checks: Every rollup entry, in the order GitHub returned them.
    """

    state: CheckState
    checks: tuple[CheckRun, ...] = ()

    @property
    def failing(self) -> tuple[CheckRun, ...]:
        """The checks that concluded red."""
        return tuple(c for c in self.checks if c.is_failing)


def _coerce_check(row: Mapping[str, Any]) -> CheckRun:
    """Build a :class:`CheckRun` from one ``statusCheckRollup`` entry.

    Handles both shapes GitHub returns: Actions ``CheckRun`` entries and
    legacy ``StatusContext`` entries.
    """
    name = str(row.get("name") or row.get("context") or "")
    url = str(row.get("detailsUrl") or row.get("targetUrl") or "")
    if "state" in row and "status" not in row:
        # Legacy StatusContext: one ``state`` field carries both meanings.
        state = str(row.get("state") or "").upper()
        if not state or state in _PENDING_STATUSES:
            status, conclusion = state or "PENDING", ""
        else:
            status, conclusion = "COMPLETED", state
    else:
        status = str(row.get("status") or "")
        conclusion = str(row.get("conclusion") or "")
    match = _RUN_ID_RE.search(url)
    return CheckRun(
        name=name,
        status=status,
        conclusion=conclusion,
        run_id=match.group(1) if match else "",
        url=url,
    )


def rollup_from_payload(payload: Mapping[str, Any]) -> CheckRollup:
    """Parse ``gh pr view --json statusCheckRollup`` output into a rollup.

    Args:
        payload: The decoded ``gh`` JSON object.

    Returns:
        The aggregate :class:`CheckRollup`.
    """
    raw: object = payload.get("statusCheckRollup")
    rows: list[object] = cast("list[object]", raw) if isinstance(raw, list) else []
    checks = tuple(_coerce_check(cast("Mapping[str, Any]", row)) for row in rows if isinstance(row, dict))
    if any(c.is_pending for c in checks):
        state: CheckState = "pending"
    elif any(c.is_failing for c in checks):
        state = "red"
    else:
        state = "green"
    return CheckRollup(state=state, checks=checks)


def wait_for_checks(
    read_rollup: Callable[[], CheckRollup],
    *,
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> CheckRollup:
    """Poll until the rollup settles, or until the budget is spent.

    The wait is bounded on purpose: an unattended review that blocks forever on
    a stuck queue is worse than one that reports ``pending`` and hands back to
    the operator.

    Args:
        read_rollup: Reads the PR's current rollup.
        timeout_s: Total wall-clock budget for the wait.
        poll_interval_s: Delay between reads.
        sleep: Injected for tests.
        monotonic: Injected for tests.

    Returns:
        The settled rollup, or the last ``pending`` one when the budget ran out.
    """
    start = monotonic()
    while True:
        rollup = read_rollup()
        if rollup.state != "pending":
            return rollup
        if monotonic() - start >= timeout_s:
            logger.info("review contour: check rollup still pending after %.0fs", timeout_s)
            return rollup
        sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# The fix pass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckLogExcerpt:
    """The failing tail of one check's log.

    Attributes:
        check: The check the excerpt came from.
        body: The log text, already truncated to the caller's byte budget.
        truncated: Whether the original log was longer than the budget.
        error: Why the log could not be fetched; empty on success.
    """

    check: str
    body: str = ""
    truncated: bool = False
    error: str = ""


@dataclass(frozen=True)
class FixRequest:
    """Everything a fix pass is told, and the prompt it renders to.

    Attributes:
        pass_index: 1-based index of the review pass that produced the verdict.
        pr_number: The PR under review, when known.
        feedback: The pipeline's summary line.
        issues: The reviewer's issues, one per entry.
        failing_checks: The checks that were red at the time of the review.
        logs: Log excerpts for those checks -- the fixer is told *why* CI is
            red rather than guessing from the diff.
        ruleset: The ruleset the verdict was produced under; its guard rules
            keep the fixer off findings an operator already rejected.
    """

    pass_index: int
    pr_number: int | None = None
    feedback: str = ""
    issues: tuple[str, ...] = ()
    failing_checks: tuple[CheckRun, ...] = ()
    logs: tuple[CheckLogExcerpt, ...] = ()
    ruleset: ReviewRuleset = EMPTY_RULESET

    def to_prompt(self) -> str:
        """Render the fix pass's input as one prompt."""
        target = f"PR #{self.pr_number}" if self.pr_number is not None else "this branch"
        lines: list[str] = [
            f"# Fix pass {self.pass_index} for {target}",
            "",
            "A review pass did not approve, or the checks are red. Land the smallest",
            "change that answers the findings below and turns the checks green, then",
            "commit and push it to the pull request's branch.",
            "",
            "## Review verdict",
            "",
            self.feedback or "(no summary)",
            "",
        ]
        if self.issues:
            lines.append("### Issues raised")
            lines.extend(f"- {issue}" for issue in self.issues)
            lines.append("")
        if self.failing_checks:
            lines.append("## Failing checks")
            lines.extend(f"- {c.name} ({c.conclusion or c.status or 'unknown'})" for c in self.failing_checks)
            lines.append("")
        for excerpt in self.logs:
            lines.append(f"### Log: {excerpt.check}")
            lines.append("")
            if excerpt.error:
                lines.append(f"(log unavailable: {excerpt.error})")
            else:
                lines.append("```")
                lines.append(excerpt.body.rstrip("\n"))
                lines.append("```")
                if excerpt.truncated:
                    lines.append("(truncated)")
            lines.append("")
        section = self.ruleset.to_prompt_section()
        if section:
            lines.append(section.lstrip("\n"))
        return "\n".join(lines)


@dataclass(frozen=True)
class FixOutcome:
    """What a fix pass reports back.

    Attributes:
        pushed: True when the pass landed a new commit on the PR's branch.
            A pass that changed nothing cannot change the next rollup, so the
            contour stops rather than burning the rest of the budget.
        summary: Short human-readable note, surfaced in the stop reason.
    """

    pushed: bool
    summary: str = ""


class FixRunner(Protocol):
    """Runs one fix pass."""

    def __call__(self, request: FixRequest, /) -> FixOutcome: ...


# ---------------------------------------------------------------------------
# Per-pass receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PassReceiptRequest:
    """The binding one pass's receipt records.

    Attributes:
        pass_index: 1-based pass index.
        diff: The exact diff bytes this pass reviewed.
        verdict: The pass's verdict.
        ruleset_digest: Digest of the ruleset the verdict was produced under.
        prev_entry_hash: The previous pass's spine anchor; empty on pass 1.
    """

    pass_index: int
    diff: bytes
    verdict: str
    ruleset_digest: str
    prev_entry_hash: str = ""
    resolution_hash: str = ""


class PassReceiptEmitter(Protocol):
    """Emits one pass's receipt and returns its spine anchor."""

    def __call__(self, request: PassReceiptRequest, /) -> str: ...


def receipt_emitter(
    *,
    workdir: Path,
    pr_url: str,
    repo: str,
    issue_body: str,
    plan: str = "",
    task_id: str = "",
    journal_head: str = "",
    hmac_key: bytes | None = None,
    timestamp: int | None = None,
) -> PassReceiptEmitter:
    """Build the emitter the contour uses for its per-pass receipts.

    Reuses the ``review-receipt`` machinery unchanged: each pass signs its own
    canonical binding with the install's Ed25519 identity, anchors it in the
    review lineage spine, and mirrors it into the HMAC-chained audit log, so
    ``bernstein review-receipt verify --chain`` recomputes the whole sequence
    offline.

    Args:
        workdir: Project root holding ``.sdd/``.
        pr_url: The pull request the receipts cover.
        repo: ``owner/repo`` slug.
        issue_body: The ticket text every pass is reviewed against.
        plan: Optional plan text bound into ``plan_hash``.
        task_id: Task the review is attributed to.
        journal_head: Run journal Merkle head, when the caller has one.
        hmac_key: Audit-chain key; loaded from the install when omitted.
        timestamp: Fixed timestamp, for reproducible fixtures.

    Returns:
        A :class:`PassReceiptEmitter` returning each receipt's spine anchor.
    """
    from bernstein.core.review.receipt import emit_review_receipt, load_or_create_review_identity
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore, record_review_receipt

    root = Path(workdir)
    key = hmac_key if hmac_key is not None else load_or_create_audit_key()
    private_pem, public_pem = load_or_create_review_identity(root / ".sdd" / "identity")

    def _emit(request: PassReceiptRequest, /) -> str:
        receipt = emit_review_receipt(
            workdir=root,
            lineage_root=root / ".sdd" / "lineage",
            hmac_key=key,
            private_key_pem=private_pem,
            public_key_pem=public_pem,
            pr_url=pr_url,
            repo=repo,
            issue_body=issue_body,
            plan=plan,
            journal_head=journal_head,
            diff=request.diff,
            findings=(),
            verdict=request.verdict,
            task_id=task_id,
            timestamp=timestamp if timestamp is not None else int(time.time()),
            pass_index=request.pass_index,
            ruleset_digest=request.ruleset_digest,
            prev_entry_hash=request.prev_entry_hash,
            resolution_hash=request.resolution_hash,
        )
        record_review_receipt(
            chain=AuditChainStore(root / ".sdd" / "audit", key=key),
            pr_url=receipt.pr_url,
            issue_hash=receipt.issue_hash,
            plan_hash=receipt.plan_hash,
            journal_head=receipt.journal_head,
            diff_hash=receipt.diff_hash,
            verdict=receipt.verdict,
            journal_entry_hash=receipt.journal_entry_hash,
        )
        return receipt.journal_entry_hash

    return _emit


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PassRecord:
    """What one pass of the contour did.

    Attributes:
        index: 1-based pass index.
        verdict: The pipeline verdict this pass produced.
        checks_state: The rollup state this pass reviewed against.
        diff_hash: Content hash of the diff this pass reviewed.
        ruleset_digest: Digest of the ruleset the verdict was produced under.
        receipt_entry_hash: The pass receipt's spine anchor; empty when no
            emitter was wired.
        failing_checks: Names of the checks that were red.
        fix_pushed: Whether the fix pass after this review landed a commit.
    """

    index: int
    verdict: FinalVerdict
    checks_state: CheckState
    diff_hash: str
    ruleset_digest: str
    receipt_entry_hash: str = ""
    failing_checks: tuple[str, ...] = ()
    fix_pushed: bool = False


@dataclass(frozen=True)
class ContourResult:
    """The contour's single outcome.

    Attributes:
        outcome: ``approved`` or ``needs-operator``.
        reason: Why the loop stopped short; empty on approval.
        passes: One record per review pass, in order.
    """

    outcome: ContourOutcome
    reason: str
    passes: tuple[PassRecord, ...]

    @property
    def exit_code(self) -> int:
        """Process exit code: 0 on approval, 1 when an operator is needed."""
        return 0 if self.outcome == "approved" else 1


def _exhausted_reason(verdict: PipelineVerdict, rollup: CheckRollup, max_passes: int) -> str:
    parts: list[str] = []
    if verdict.verdict != "approve":
        parts.append("the review still requests changes")
    if rollup.state != "green":
        parts.append(f"the checks are {rollup.state}")
    detail = " and ".join(parts) or "the contour did not converge"
    return f"budget spent (max_passes={max_passes}): {detail}"


def run_review_contour(
    pipeline: ReviewPipeline,
    *,
    fetch_diff: Callable[[], DiffSource],
    read_rollup: Callable[[], CheckRollup],
    review: Callable[[DiffSource], PipelineVerdict],
    ruleset: ReviewRuleset = EMPTY_RULESET,
    fetch_logs: Callable[[CheckRun], CheckLogExcerpt] | None = None,
    fix_runner: FixRunner | None = None,
    emit_receipt: PassReceiptEmitter | None = None,
    max_passes: int = 3,
    until_checks_green: bool = True,
    settle_timeout_s: float = 900.0,
    poll_interval_s: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    pr_number: int | None = None,
) -> ContourResult:
    """Run review -> fix -> re-check until it converges or the budget is spent.

    Args:
        pipeline: The validated review pipeline whose passes are being run.
        fetch_diff: Reads the PR's current diff.
        read_rollup: Reads the PR's current check rollup.
        review: Runs the pipeline against a diff.
        ruleset: The ruleset the verdicts are produced under.
        fetch_logs: Fetches one failing check's log excerpt.  Without it the
            fix pass is told which checks failed but not why.
        fix_runner: Runs one fix pass.  Without it the contour reviews once
            and, unless that single pass already approves with green checks,
            hands back to the operator rather than approving anyway.
        emit_receipt: Emits one receipt per pass.
        max_passes: Review budget; at least 1.
        until_checks_green: When true, an approval also requires green checks.
        settle_timeout_s: Bound on each wait for the rollup to settle.
        poll_interval_s: Delay between rollup reads.
        sleep: Injected for tests.
        monotonic: Injected for tests.
        pr_number: PR number, for prompts and logs.

    Returns:
        A :class:`ContourResult` whose ``exit_code`` is non-zero unless the
        contour approved.

    Raises:
        ValueError: If ``max_passes`` is below 1.
    """
    from bernstein.core.review.receipt import compute_diff_hash

    if max_passes < 1:
        raise ValueError(f"max_passes must be at least 1, got {max_passes}")

    records: list[PassRecord] = []
    previous_anchor = ""
    reason = ""

    for index in range(1, max_passes + 1):
        rollup = wait_for_checks(
            read_rollup,
            timeout_s=settle_timeout_s,
            poll_interval_s=poll_interval_s,
            sleep=sleep,
            monotonic=monotonic,
        )
        diff_src = fetch_diff()
        verdict = review(diff_src)
        diff_bytes = diff_src.diff.encode("utf-8")

        anchor = ""
        if emit_receipt is not None:
            anchor = emit_receipt(
                PassReceiptRequest(
                    pass_index=index,
                    diff=diff_bytes,
                    verdict=verdict.verdict,
                    ruleset_digest=ruleset.digest,
                    prev_entry_hash=previous_anchor,
                )
            )
            previous_anchor = anchor

        record = PassRecord(
            index=index,
            verdict=verdict.verdict,
            checks_state=rollup.state,
            diff_hash=compute_diff_hash(diff_bytes),
            ruleset_digest=ruleset.digest,
            receipt_entry_hash=anchor,
            failing_checks=tuple(c.name for c in rollup.failing),
        )
        logger.info(
            "review contour: %s pass %d/%d verdict=%s checks=%s",
            pipeline.name or "<unnamed>",
            index,
            max_passes,
            verdict.verdict,
            rollup.state,
        )

        if verdict.verdict == "approve" and (rollup.state == "green" or not until_checks_green):
            records.append(record)
            return ContourResult(outcome="approved", reason="", passes=tuple(records))

        if index == max_passes:
            records.append(record)
            reason = _exhausted_reason(verdict, rollup, max_passes)
            break

        if fix_runner is None:
            records.append(record)
            reason = "no fix runner configured; pass --fix with a fix command to let the contour continue"
            break

        request = FixRequest(
            pass_index=index,
            pr_number=pr_number if pr_number is not None else diff_src.pr_number,
            feedback=verdict.feedback,
            issues=tuple(verdict.issues),
            failing_checks=rollup.failing,
            logs=_collect_logs(rollup.failing, fetch_logs),
            ruleset=ruleset,
        )
        outcome = fix_runner(request)
        records.append(replace(record, fix_pushed=outcome.pushed))
        if not outcome.pushed:
            reason = f"fix pass {index} landed no commit: {outcome.summary or 'no detail reported'}"
            break

    return ContourResult(outcome="needs-operator", reason=reason, passes=tuple(records))


def _collect_logs(
    checks: Sequence[CheckRun],
    fetch_logs: Callable[[CheckRun], CheckLogExcerpt] | None,
) -> tuple[CheckLogExcerpt, ...]:
    """Fetch one log excerpt per failing check, tolerating fetch failures."""
    if fetch_logs is None:
        return ()
    out: list[CheckLogExcerpt] = []
    for check in checks:
        try:
            out.append(fetch_logs(check))
        except (OSError, RuntimeError) as exc:
            logger.warning("review contour: could not fetch log for %s: %s", check.name, exc)
            out.append(CheckLogExcerpt(check=check.name, error=str(exc)))
    return tuple(out)


# ---------------------------------------------------------------------------
# Default collaborators (the CLI wires these; the loop above stays pure)
# ---------------------------------------------------------------------------


def check_log_fetcher(
    *,
    repo: str | None = None,
    byte_budget: int = DEFAULT_LOG_BYTE_BUDGET,
) -> Callable[[CheckRun], CheckLogExcerpt]:
    """Return a log fetcher backed by the existing ``gh run view`` wrapper.

    Args:
        repo: ``owner/name`` override passed to ``gh``.
        byte_budget: Hard cap on one check's excerpt.

    Returns:
        A callable mapping a failing :class:`CheckRun` to its log excerpt.
    """
    from bernstein.core.autofix.gh_logs import extract_failed_log

    def _fetch(check: CheckRun) -> CheckLogExcerpt:
        if not check.run_id:
            return CheckLogExcerpt(check=check.name, error="check has no workflow run id")
        extraction = extract_failed_log(check.run_id, byte_budget=byte_budget, repo=repo)
        return CheckLogExcerpt(
            check=check.name,
            body=extraction.body,
            truncated=extraction.truncated,
            error=extraction.error,
        )

    return _fetch


def command_fix_runner(
    command: str,
    *,
    repo_root: Path,
    timeout_s: float = 3600.0,
) -> FixRunner:
    """Run an operator-supplied command as the fix pass.

    The command is invoked with the rendered fix prompt appended as its last
    argument (via a file path, so a multi-kilobyte prompt survives the argv
    limit).  The pass counts as pushed only when the command exits 0 *and*
    ``HEAD`` moved -- a command that changed nothing cannot change the next
    check rollup, and the contour stops rather than looping over it.

    Args:
        command: Shell-quoted command; split with :func:`shlex.split`.
        repo_root: Repository the command runs in.
        timeout_s: Per-pass timeout.

    Returns:
        A :class:`FixRunner`.
    """
    argv_prefix = shlex.split(command)

    def _head() -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.stdout.strip()

    def _run(request: FixRequest, /) -> FixOutcome:
        prompt_path = repo_root / ".sdd" / "runtime" / f"review-fix-pass-{request.pass_index}.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(request.to_prompt(), encoding="utf-8")
        before = _head()
        try:
            proc = subprocess.run(
                [*argv_prefix, str(prompt_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return FixOutcome(pushed=False, summary=f"fix command timed out after {timeout_s:.0f}s")
        except OSError as exc:
            return FixOutcome(pushed=False, summary=f"could not run the fix command: {exc}")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return FixOutcome(
                pushed=False,
                summary=f"fix command exited {proc.returncode}: {detail[-1] if detail else 'no output'}",
            )
        after = _head()
        if after == before:
            return FixOutcome(pushed=False, summary="fix command produced no commit")
        return FixOutcome(pushed=True, summary=f"landed {after[:12]}")

    return _run
