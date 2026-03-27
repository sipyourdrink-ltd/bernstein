"""Evolution cycle helper logic — community, creative, and GitHub sync.

Extracted from cycle_runner.py to keep cycle_runner.py under the 800-line
hard limit.  All helpers operate on an EvolutionLoop-like host object passed
as ``self`` (they are mixed into EvolutionLoop via inheritance).

Public surface:
  - CommunityCreativeMixin — community/creative cycle runners + GitHub helpers
  - run_creative_cycle      — standalone function (delegates to mixin)
  - run_community_cycle     — standalone function (delegates to mixin)
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from bernstein.evolution.creative import (
    AnalystVerdict,
    PipelineResult,
    VisionaryProposal,
    issue_to_proposal,
)
from bernstein.evolution.types import RiskLevel

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.github import GitHubClient

logger = logging.getLogger(__name__)

# Cost estimate per proposal generation (LLM call) — mirrors cycle_runner constant.
_COST_PER_PROPOSAL_USD = 0.05


# ---------------------------------------------------------------------------
# Data class re-used across helpers (imported here to avoid circular import)
# ---------------------------------------------------------------------------

# ExperimentResult is defined in cycle_runner.py; helpers receive / return it
# by TYPE_CHECKING only to avoid circular imports.
if TYPE_CHECKING:
    from bernstein.evolution.cycle_runner import ExperimentResult


class CommunityCreativeMixin:
    """Mixin providing community-issue and creative-vision cycle runners.

    Expects the following attributes on ``self`` (provided by EvolutionLoop):
      _evolution_dir: Path
      _creative_pipeline: CreativePipeline
      _cycle_count: int
      _github_sync: bool
      _github: GitHubClient | None
      _experiments_path: Path
      _current_issue_number: int | None

    And the following methods:
      _log_experiment(result) → None
    """

    # Declared here for type-checking; actually set by EvolutionLoop.__init__
    _evolution_dir: Path
    _cycle_count: int
    _github_sync: bool
    _github: Any | None
    _experiments_path: Path
    _current_issue_number: int | None

    # -----------------------------------------------------------------------
    # Creative cycle
    # -----------------------------------------------------------------------

    def _run_creative_cycle(self, cycle_start: float) -> ExperimentResult | None:
        """Run a creative vision cycle via the three-stage pipeline.

        Reads pending visionary proposals and analyst verdicts from
        ``.sdd/evolution/creative/pending_proposals.jsonl``.  Each line must
        be a JSON object with ``"proposal"`` and ``"verdict"`` keys.  The loop
        drains the file on each creative turn and runs the production gate.

        Args:
            cycle_start: Unix timestamp when this cycle started.

        Returns:
            ExperimentResult summarising the creative run, or None if no
            pending proposals were found.
        """
        from bernstein.evolution.cycle_runner import ExperimentResult  # local to avoid circular

        pending_path = self._evolution_dir / "creative" / "pending_proposals.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)

        if not pending_path.exists() or pending_path.stat().st_size == 0:
            logger.debug("Creative vision: no pending proposals — skipping cycle")
            return None

        proposals: list[VisionaryProposal] = []
        verdicts: list[AnalystVerdict] = []

        try:
            raw_lines = pending_path.read_text(encoding="utf-8").strip().splitlines()
            for line in raw_lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                if "proposal" in record:
                    proposals.append(VisionaryProposal.from_dict(record["proposal"]))
                if "verdict" in record:
                    verdicts.append(AnalystVerdict.from_dict(record["verdict"]))
            # Drain the file so proposals are not re-processed next cycle.
            pending_path.write_text("", encoding="utf-8")
        except (OSError, json.JSONDecodeError, KeyError):
            logger.exception("Creative vision: failed to read pending proposals")
            return None

        if not proposals and not verdicts:
            return None

        logger.info(
            "Creative vision: running pipeline with %d proposal(s), %d verdict(s)",
            len(proposals),
            len(verdicts),
        )

        pipeline_result: PipelineResult = self._creative_pipeline.run(  # type: ignore[attr-defined]
            proposals,
            verdicts,
        )

        approved_count = len(pipeline_result.approved)
        tasks_count = len(pipeline_result.tasks_created)
        accepted = approved_count > 0

        logger.info(
            "Creative vision: %d approved, %d backlog task(s) created",
            approved_count,
            tasks_count,
        )

        result = ExperimentResult(
            proposal_id=f"creative-{self._cycle_count}",
            title=f"Creative vision cycle ({len(proposals)} proposals)",
            risk_level=RiskLevel.L1_TEMPLATE.value,
            baseline_score=1.0,
            candidate_score=1.0 + (0.01 * approved_count),
            delta=0.01 * approved_count,
            accepted=accepted,
            reason=(f"{approved_count}/{len(verdicts)} approved, {tasks_count} backlog task(s) created"),
            cost_usd=_COST_PER_PROPOSAL_USD * max(1, len(proposals)),
            duration_seconds=time.time() - cycle_start,
        )
        self._log_experiment(result)  # type: ignore[attr-defined]
        return result

    # -----------------------------------------------------------------------
    # Community cycle
    # -----------------------------------------------------------------------

    def _run_community_cycle(self, cycle_start: float) -> ExperimentResult | None:
        """Process the highest-priority community-requested issue.

        Fetches open ``evolve-candidate`` / ``feature-request`` issues from
        GitHub, sorted by 👍 reaction count.  The top issue that passes the
        trust check (collaborator author or ``maintainer-approved`` label) is
        converted to a ``VisionaryProposal``, pushed through the analyst gate,
        and written to the backlog for the main orchestrator.

        Marks the issue as ``evolve-in-progress`` on start, and closes it
        when the backlog task is created successfully.

        Args:
            cycle_start: Unix timestamp when this cycle started.

        Returns:
            ExperimentResult summarising the community cycle, or None if no
            eligible community issues were found.
        """
        from bernstein.evolution.cycle_runner import ExperimentResult  # local to avoid circular

        gh = self._gh  # type: ignore[attr-defined]
        if gh is None or not gh.available:
            logger.debug("Community cycle: GitHub unavailable — skipping")
            return None

        issues = gh.fetch_community_issues()
        if not issues:
            logger.debug("Community cycle: no eligible community issues found")
            return None

        # Pick the first issue that passes the trust check.
        selected = None
        for issue in issues:
            if issue.is_maintainer_approved:
                selected = issue
                break
            if issue.author and gh.check_is_collaborator(issue.author):
                selected = issue
                break

        if selected is None:
            logger.info(
                "Community cycle: %d issue(s) found but none passed trust check "
                "(no collaborator author and no maintainer-approved label)",
                len(issues),
            )
            return None

        logger.info(
            "Community cycle: processing issue #%d '%s' (%d 👍)",
            selected.number,
            selected.title,
            selected.thumbs_up,
        )

        # Mark in-progress so other instances skip this issue.
        gh.mark_in_progress(selected.number)

        # Convert the issue to a visionary proposal.
        proposal = issue_to_proposal(selected)

        # Run the analyst + production gate via the creative pipeline.
        # We create a synthetic AnalystVerdict that auto-approves community
        # issues with a reasonable baseline score.  The human can always
        # reject the resulting backlog task or PR.
        analyst_verdict = AnalystVerdict(
            proposal_title=proposal.title,
            verdict="APPROVE",
            feasibility_score=7.0,
            impact_score=8.0,
            risk_score=4.0,
            composite_score=AnalystVerdict.compute_composite(7.0, 8.0, 4.0),
            reasoning=(
                f"Community-requested feature from GitHub issue #{selected.number}. "
                f"Thumbs-up: {selected.thumbs_up}. Auto-approved for backlog creation."
            ),
            decomposition=[],
        )

        pipeline_result: PipelineResult = self._creative_pipeline.run(  # type: ignore[attr-defined]
            [proposal],
            [analyst_verdict],
        )

        tasks_created = len(pipeline_result.tasks_created)
        accepted = tasks_created > 0

        if accepted:
            logger.info(
                "Community cycle: created %d backlog task(s) for issue #%d",
                tasks_created,
                selected.number,
            )
            # Close the GitHub issue now that a backlog task exists.
            closing_comment = (
                "Bernstein has created a backlog task for this request. "
                "Implementation will be tracked internally.\n\n"
                "*Processed by `bernstein evolve run --community`*"
            )
            gh.close_issue(selected.number, comment=closing_comment)
        else:
            # Pipeline rejected or no tasks — unmark so it can be retried.
            gh.unmark_in_progress(selected.number)
            logger.info("Community cycle: pipeline produced no tasks for issue #%d", selected.number)

        result = ExperimentResult(
            proposal_id=f"community-{selected.number}",
            title=f"Community issue #{selected.number}: {selected.title}",
            risk_level=RiskLevel.L1_TEMPLATE.value,
            baseline_score=1.0,
            candidate_score=1.0 + (0.01 * tasks_created),
            delta=0.01 * tasks_created,
            accepted=accepted,
            reason=(f"{tasks_created} backlog task(s) created" if accepted else "Pipeline produced no tasks"),
            cost_usd=_COST_PER_PROPOSAL_USD,
            duration_seconds=time.time() - cycle_start,
        )
        self._log_experiment(result)  # type: ignore[attr-defined]
        return result

    # -----------------------------------------------------------------------
    # GitHub sync helpers
    # -----------------------------------------------------------------------

    @property
    def _gh(self) -> GitHubClient | None:
        """Return the lazily-initialised GitHubClient, or None if sync disabled.

        Deferred import keeps the ``gh`` CLI optional — the evolution loop
        works without it.
        """
        if not self._github_sync:
            return None
        if self._github is None:
            from bernstein.core.github import GitHubClient

            self._github = GitHubClient()
            if not self._github.available:
                logger.warning(
                    "GitHub sync requested but gh CLI is unavailable or unauthenticated — running without GitHub sync"
                )
        return self._github

    def _github_check_unclaimed(self) -> str | None:
        """Check GitHub for unclaimed evolution issues before generating.

        If an unclaimed issue exists, claim it and track its number so we
        can close it on success.  Returns the issue title as a hint (for
        logging purposes only — the proposal generator still runs normally).

        Returns:
            Title of the claimed issue, or None if none available or GitHub
            sync is disabled / unavailable.
        """
        gh = self._gh
        if gh is None or not gh.available:
            return None

        unclaimed = gh.find_unclaimed()
        if not unclaimed:
            return None

        issue = unclaimed[0]
        logger.info(
            "GitHub sync: claiming existing issue #%d '%s'",
            issue.number,
            issue.title,
        )
        claimed = gh.claim_issue(issue.number)
        if claimed:
            self._current_issue_number = issue.number
        return issue.title

    def _github_sync_proposal(self, title: str, description: str) -> None:
        """Publish a new proposal as a GitHub issue, or claim an existing one.

        If an issue with the same title hash already exists (from another
        instance), claim that issue rather than creating a duplicate.

        Args:
            title: Proposal title.
            description: Proposal description for the issue body.
        """
        gh = self._gh
        if gh is None or not gh.available:
            return

        # If we already claimed an issue in _github_check_unclaimed, skip.
        if self._current_issue_number is not None:
            return

        # Check for a duplicate by title hash.
        existing = gh.find_by_hash(title)
        if existing is not None:
            logger.info(
                "GitHub sync: duplicate detected — claiming existing issue #%d",
                existing.number,
            )
            if gh.claim_issue(existing.number):
                self._current_issue_number = existing.number
            return

        # No duplicate — create a new issue.
        body = (
            f"## Auto-generated evolution proposal\n\n"
            f"{description}\n\n"
            f"---\n"
            f"*Generated by `bernstein evolve run --github`*"
        )
        issue = gh.create_issue(title=title, body=body)
        if issue is not None:
            logger.info(
                "GitHub sync: created issue #%d for proposal '%s'",
                issue.number,
                title,
            )
            if gh.claim_issue(issue.number):
                self._current_issue_number = issue.number

    def _github_close_current(self, comment: str | None = None) -> None:
        """Close the currently tracked GitHub issue.

        Args:
            comment: Optional closing comment.
        """
        gh = self._gh
        if gh is None or self._current_issue_number is None:
            return
        closed = gh.close_issue(self._current_issue_number, comment=comment)
        if closed:
            logger.info(
                "GitHub sync: closed issue #%d",
                self._current_issue_number,
            )
        self._current_issue_number = None

    def _github_unclaim_current(self) -> None:
        """Unclaim the currently tracked GitHub issue.

        Called when the proposal is deferred or blocked so another instance
        can pick it up.
        """
        gh = self._gh
        if gh is None or self._current_issue_number is None:
            return
        gh.unclaim_issue(self._current_issue_number)
        logger.info(
            "GitHub sync: unclaimed issue #%d",
            self._current_issue_number,
        )
        self._current_issue_number = None
