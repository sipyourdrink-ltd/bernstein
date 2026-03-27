"""Creative evolution pipeline — visionary → analyst → production gate.

Three-stage pipeline that generates bold feature ideas (visionary),
evaluates them ruthlessly (analyst), and converts approved proposals
into backlog tasks for the normal orchestrator flow.

Replaces the incremental self-evolve loop's ``creative_vision`` focus
with genuine product thinking.

Community evolve extension:
    ``issue_to_proposal`` converts a GitHub community issue (fetched via
    ``GitHubClient``) into a ``VisionaryProposal`` so it can enter the
    same analyst/gate pipeline as internally-generated ideas.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.github import GitHubIssue

logger = logging.getLogger(__name__)

# Minimum composite score for a proposal to be approved.
_APPROVAL_THRESHOLD = 7.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class VisionaryProposal:
    """A bold feature idea from the visionary stage.

    Attributes:
        title: One-line pitch.
        why: The user problem it solves.
        what: Concrete feature description.
        impact: How it changes the user experience.
        risk: What could go wrong.
        effort_estimate: S, M, or L.
    """

    title: str
    why: str
    what: str
    impact: str
    risk: str
    effort_estimate: Literal["S", "M", "L"]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VisionaryProposal:
        """Deserialize from a dict.

        Args:
            raw: Dict with proposal fields.

        Returns:
            Populated VisionaryProposal.
        """
        effort = raw.get("effort_estimate", "M")
        if effort not in ("S", "M", "L"):
            effort = "M"
        return cls(
            title=raw["title"],
            why=raw["why"],
            what=raw["what"],
            impact=raw["impact"],
            risk=raw["risk"],
            effort_estimate=effort,
        )


@dataclass
class AnalystVerdict:
    """Evaluation of a visionary proposal by the analyst stage.

    Attributes:
        proposal_title: Title of the evaluated proposal.
        verdict: APPROVE, REVISE, or REJECT.
        feasibility_score: 1-10 technical feasibility.
        impact_score: 1-10 user impact.
        risk_score: 1-10 (higher = riskier).
        composite_score: Weighted combination capped to 0-10.
        reasoning: 2-3 sentence explanation.
        revisions: Specific changes if REVISE.
        decomposition: Concrete tasks if APPROVE.
    """

    proposal_title: str
    verdict: Literal["APPROVE", "REVISE", "REJECT"]
    feasibility_score: float
    impact_score: float
    risk_score: float
    composite_score: float
    reasoning: str
    revisions: list[str] = field(default_factory=list[str])
    decomposition: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AnalystVerdict:
        """Deserialize from a dict.

        Args:
            raw: Dict with verdict fields.

        Returns:
            Populated AnalystVerdict.
        """
        verdict = raw.get("verdict", "REJECT")
        if verdict not in ("APPROVE", "REVISE", "REJECT"):
            verdict = "REJECT"
        return cls(
            proposal_title=raw.get("proposal_title", ""),
            verdict=verdict,
            feasibility_score=float(raw.get("feasibility_score", 0)),
            impact_score=float(raw.get("impact_score", 0)),
            risk_score=float(raw.get("risk_score", 0)),
            composite_score=float(raw.get("composite_score", 0)),
            reasoning=raw.get("reasoning", ""),
            revisions=list(raw.get("revisions", [])),
            decomposition=list(raw.get("decomposition", [])),
        )

    @classmethod
    def compute_composite(
        cls,
        feasibility: float,
        impact: float,
        risk: float,
    ) -> float:
        """Compute composite score from component scores.

        Formula: (0.4 * feasibility + 0.4 * impact - 0.2 * risk) * 10 / 8
        Clamped to [0, 10].

        Args:
            feasibility: 1-10 score.
            impact: 1-10 score.
            risk: 1-10 score (higher = riskier).

        Returns:
            Composite score in [0, 10].
        """
        raw = (0.4 * feasibility + 0.4 * impact - 0.2 * risk) * 10 / 8
        return max(0.0, min(10.0, round(raw, 2)))


# ---------------------------------------------------------------------------
# Community issue conversion
# ---------------------------------------------------------------------------


def issue_to_proposal(issue: GitHubIssue) -> VisionaryProposal:
    """Convert a community GitHub issue into a VisionaryProposal.

    Extracts structured fields from the issue body when they follow the
    ``evolve-candidate`` template format.  Falls back to sensible defaults
    derived from the title and body text for free-form issues.

    Args:
        issue: A ``GitHubIssue`` fetched via ``GitHubClient.fetch_community_issues``.

    Returns:
        A ``VisionaryProposal`` that can enter the analyst / production gate
        pipeline as if it were generated by the visionary stage.
    """
    body = issue.body or ""

    # Try to extract structured sections from the evolve-candidate template.
    # Template headings: "### Problem", "### Proposed solution", "### Impact",
    # "### Risk", "### Effort".
    def _extract_section(heading: str, text: str) -> str:
        pattern = rf"###\s+{re.escape(heading)}\s*\n(.*?)(?=\n###|\Z)"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    why = _extract_section("Problem", body) or _extract_section("Why", body)
    what = _extract_section("Proposed solution", body) or _extract_section("What", body)
    impact = _extract_section("Impact", body)
    risk = _extract_section("Risk", body)
    effort_raw = _extract_section("Effort", body).upper()

    # Fall back to synthesising from title + full body when the template
    # fields are missing (free-form issues).
    if not why:
        why = f"Community request: {issue.title}"
    if not what:
        # Use first non-empty paragraph of the body as the description.
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        what = paragraphs[0] if paragraphs else issue.title
    if not impact:
        votes_note = f" ({issue.thumbs_up} 👍)" if issue.thumbs_up else ""
        impact = f"Addresses community-reported need{votes_note}: {issue.title}"
    if not risk:
        risk = "Unknown — community issue; requires feasibility review."

    effort: Literal["S", "M", "L"] = "M"
    if effort_raw.startswith("S"):
        effort = "S"
    elif effort_raw.startswith("L"):
        effort = "L"

    return VisionaryProposal(
        title=issue.title,
        why=why,
        what=what,
        impact=impact,
        risk=risk,
        effort_estimate=effort,
    )


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Outcome of a full creative pipeline run.

    Attributes:
        proposals: Raw visionary proposals.
        verdicts: Analyst evaluations.
        approved: Proposals that passed the production gate.
        tasks_created: Paths to backlog tasks written.
        timestamp: Unix timestamp of the run.
    """

    proposals: list[VisionaryProposal]
    verdicts: list[AnalystVerdict]
    approved: list[AnalystVerdict]
    tasks_created: list[Path]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "proposals": [p.to_dict() for p in self.proposals],
            "verdicts": [v.to_dict() for v in self.verdicts],
            "approved_count": len(self.approved),
            "tasks_created": [str(p) for p in self.tasks_created],
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# CreativePipeline
# ---------------------------------------------------------------------------


class CreativePipeline:
    """Three-stage creative evolution pipeline.

    Stage 1: Visionary — generates bold feature proposals.
    Stage 2: Analyst — evaluates proposals with scoring.
    Stage 3: Production gate — converts approved proposals to backlog tasks.

    The pipeline itself is deterministic code. The visionary and analyst
    stages are intended to be driven by LLM agents, but the pipeline
    accepts their output as structured data so it can run without agents
    (e.g. in tests or with pre-written proposals).

    Args:
        state_dir: Path to the .sdd directory.
        repo_root: Repository root. Defaults to state_dir.parent.
        approval_threshold: Minimum composite_score for APPROVE (default 7.0).
        github_sync: If True, create GitHub Issues for each approved proposal
            so the community can track and vote on self-improvement work.
            Requires the ``gh`` CLI to be installed and authenticated.
    """

    def __init__(
        self,
        state_dir: Path,
        repo_root: Path | None = None,
        approval_threshold: float = _APPROVAL_THRESHOLD,
        github_sync: bool = False,
    ) -> None:
        self._state_dir = state_dir
        self._repo_root = repo_root or state_dir.parent
        self._approval_threshold = approval_threshold
        self._creative_dir = state_dir / "evolution" / "creative"
        self._creative_dir.mkdir(parents=True, exist_ok=True)
        self._backlog_dir = state_dir / "backlog"
        self._github_sync = github_sync
        self._github: object | None = None  # GitHubClient, lazily initialised

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        proposals: list[VisionaryProposal],
        verdicts: list[AnalystVerdict],
        *,
        dry_run: bool = False,
    ) -> PipelineResult:
        """Run the production gate on pre-evaluated proposals.

        Filters verdicts to those that meet the approval threshold,
        converts approved proposals to backlog tasks, and logs the run.

        Args:
            proposals: Visionary proposals (stage 1 output).
            verdicts: Analyst verdicts (stage 2 output).
            dry_run: If True, skip writing backlog tasks.

        Returns:
            PipelineResult with approved proposals and created tasks.
        """
        approved = self._filter_approved(verdicts)
        tasks_created: list[Path] = []

        if not dry_run:
            tasks_created = self._create_backlog_tasks(proposals, approved)
            if self._github_sync and approved:
                self._publish_approved_to_github(proposals, approved)

        result = PipelineResult(
            proposals=proposals,
            verdicts=verdicts,
            approved=approved,
            tasks_created=tasks_created,
        )

        self._log_run(result)
        return result

    def filter_approved(
        self,
        verdicts: list[AnalystVerdict],
    ) -> list[AnalystVerdict]:
        """Public access to filtering logic.

        Args:
            verdicts: Analyst verdicts to filter.

        Returns:
            Verdicts that pass the approval threshold.
        """
        return self._filter_approved(verdicts)

    def create_backlog_tasks(
        self,
        proposals: list[VisionaryProposal],
        approved_verdicts: list[AnalystVerdict],
    ) -> list[Path]:
        """Public access to task creation.

        Args:
            proposals: All visionary proposals (for context lookup).
            approved_verdicts: Only the approved verdicts.

        Returns:
            Paths to created backlog task files.
        """
        return self._create_backlog_tasks(proposals, approved_verdicts)

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Read recent creative pipeline runs from the log.

        Args:
            limit: Maximum number of runs to return.

        Returns:
            List of run dicts, most recent first.
        """
        log_path = self._creative_dir / "runs.jsonl"
        if not log_path.exists():
            return []

        runs: list[dict[str, Any]] = []
        try:
            for line in log_path.read_text(encoding="utf-8").strip().splitlines():
                if line.strip():
                    runs.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to read creative pipeline log")
            return []

        runs.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return runs[:limit]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filter_approved(
        self,
        verdicts: list[AnalystVerdict],
    ) -> list[AnalystVerdict]:
        """Filter verdicts to those that meet the approval threshold.

        A verdict must have verdict == "APPROVE" AND composite_score
        >= self._approval_threshold.

        Args:
            verdicts: All analyst verdicts.

        Returns:
            List of approved verdicts.
        """
        return [v for v in verdicts if v.verdict == "APPROVE" and v.composite_score >= self._approval_threshold]

    def _create_backlog_tasks(
        self,
        proposals: list[VisionaryProposal],
        approved_verdicts: list[AnalystVerdict],
    ) -> list[Path]:
        """Convert approved verdicts into backlog task files.

        Each decomposition item from an approved verdict becomes a
        separate backlog task. The original proposal context is included
        in the task description.

        Args:
            proposals: All visionary proposals (indexed by title for lookup).
            approved_verdicts: Approved analyst verdicts.

        Returns:
            Paths to created backlog files.
        """
        open_dir = self._backlog_dir / "open"
        open_dir.mkdir(parents=True, exist_ok=True)

        # Build title → proposal lookup.
        proposal_map: dict[str, VisionaryProposal] = {p.title: p for p in proposals}

        next_id = self._next_ticket_id()
        created: list[Path] = []

        for verdict in approved_verdicts:
            proposal = proposal_map.get(verdict.proposal_title)
            context_block = ""
            if proposal:
                context_block = (
                    f"## Vision context\n\n"
                    f"**Why:** {proposal.why}\n"
                    f"**What:** {proposal.what}\n"
                    f"**Impact:** {proposal.impact}\n"
                    f"**Risk:** {proposal.risk}\n"
                    f"**Effort:** {proposal.effort_estimate}\n\n"
                    f"## Analyst evaluation\n\n"
                    f"**Composite score:** {verdict.composite_score}\n"
                    f"**Reasoning:** {verdict.reasoning}\n\n"
                )

            if not verdict.decomposition:
                # No decomposition — create a single task for the whole proposal.
                path = self._write_task(
                    ticket_id=str(next_id),
                    title=verdict.proposal_title,
                    description=context_block,
                    role="backend",
                    priority=2,
                    scope="medium",
                    complexity="high",
                    open_dir=open_dir,
                )
                created.append(path)
                next_id += 1
            else:
                for task_desc in verdict.decomposition:
                    path = self._write_task(
                        ticket_id=str(next_id),
                        title=task_desc,
                        description=(f"Part of: **{verdict.proposal_title}**\n\n{context_block}"),
                        role="backend",
                        priority=2,
                        scope="medium",
                        complexity="medium",
                        open_dir=open_dir,
                    )
                    created.append(path)
                    next_id += 1

        return created

    def _write_task(
        self,
        *,
        ticket_id: str,
        title: str,
        description: str,
        role: str,
        priority: int,
        scope: str,
        complexity: str,
        open_dir: Path,
    ) -> Path:
        """Write a single backlog task file.

        Args:
            ticket_id: Numeric ticket ID.
            title: Task title.
            description: Task description body.
            role: Assigned role.
            priority: Task priority (1-3).
            scope: small/medium/large.
            complexity: low/medium/high.
            open_dir: Directory to write the task into.

        Returns:
            Path to the written file.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
        filename = f"{ticket_id}-{slug}.md"
        file_path = open_dir / filename
        content = (
            f"# {ticket_id} — {title}\n\n"
            f"**Role:** {role}\n"
            f"**Priority:** {priority}\n"
            f"**Scope:** {scope}\n"
            f"**Complexity:** {complexity}\n\n"
            f"## Description\n\n"
            f"{description}\n\n"
            f"<!-- source: creative-pipeline -->\n"
        )
        file_path.write_text(content, encoding="utf-8")
        logger.info("Created backlog task %s: %s", ticket_id, title)
        return file_path

    def _next_ticket_id(self) -> int:
        """Return the next available numeric ticket ID.

        Scans both open and done backlog directories for existing IDs.

        Returns:
            Next integer ID.
        """
        max_id = 0
        for subdir in ("open", "done"):
            d = self._backlog_dir / subdir
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                m = re.match(r"(\d+)", f.name)
                if m:
                    max_id = max(max_id, int(m.group(1)))
        return max_id + 1

    def _publish_approved_to_github(
        self,
        proposals: list[VisionaryProposal],
        approved_verdicts: list[AnalystVerdict],
    ) -> None:
        """Create GitHub Issues for approved creative proposals.

        Creates one issue per approved verdict (not per decomposition task).
        Issues are labelled ``bernstein-evolve`` and ``auto-generated`` and
        carry a ``evolve-hash-*`` dedup label so parallel instances won't
        open duplicates.

        Skips quietly if the ``gh`` CLI is unavailable or unauthenticated.

        Args:
            proposals: All visionary proposals (for body context).
            approved_verdicts: Only the approved analyst verdicts.
        """
        # Deferred import keeps gh CLI optional.
        from bernstein.core.github import GitHubClient

        if self._github is None:
            self._github = GitHubClient()

        gh: GitHubClient = self._github  # type: ignore[assignment]
        if not gh.available:
            logger.debug("Creative pipeline: gh CLI unavailable — skipping GitHub sync")
            return

        proposal_map = {p.title: p for p in proposals}

        for verdict in approved_verdicts:
            proposal = proposal_map.get(verdict.proposal_title)

            # Skip if an issue with the same title already exists.
            existing = gh.find_by_hash(verdict.proposal_title)
            if existing is not None:
                logger.debug(
                    "Creative pipeline: issue #%d already exists for '%s' — skipping",
                    existing.number,
                    verdict.proposal_title,
                )
                continue

            body_parts = [
                f"## {verdict.proposal_title}",
                "",
                f"**Composite score:** {verdict.composite_score:.1f}/10",
                f"**Reasoning:** {verdict.reasoning}",
            ]
            if proposal:
                body_parts += [
                    "",
                    "### Vision",
                    f"**Why:** {proposal.why}",
                    f"**What:** {proposal.what}",
                    f"**Impact:** {proposal.impact}",
                    f"**Effort:** {proposal.effort_estimate}",
                ]
            if verdict.decomposition:
                body_parts += ["", "### Decomposition"]
                body_parts += [f"- {task}" for task in verdict.decomposition]
            body_parts += [
                "",
                "---",
                "*Generated by `bernstein evolve run --github` — creative pipeline*",
            ]
            body = "\n".join(body_parts)

            issue = gh.create_issue(title=verdict.proposal_title, body=body)
            if issue is not None:
                logger.info(
                    "Creative pipeline: published issue #%d '%s'",
                    issue.number,
                    verdict.proposal_title,
                )

    def _log_run(self, result: PipelineResult) -> None:
        """Append run result to the creative pipeline JSONL log.

        Args:
            result: Pipeline result to log.
        """
        log_path = self._creative_dir / "runs.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
        except OSError:
            logger.exception("Failed to write creative pipeline log")
