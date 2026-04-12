"""Coordinator synthesis between parallel workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SynthesisResult:
    """Result of synthesizing worker outputs."""

    synthesized_summary: str
    worker_count: int
    successful_workers: int
    failed_workers: int
    artifacts_merged: list[str]
    conflicts_detected: list[str]


class SynthesisEngine:
    """Synthesize outputs from parallel workers into coherent summary.

    After parallel workers finish, runs a synthesis step that:
    1. Collects worker artifacts and summaries
    2. Merges findings into a single coherent summary
    3. Detects and reports any conflicts
    4. Attaches synthesized result to parent task

    Args:
        workdir: Project working directory.
        use_llm: Whether to use LLM for synthesis (default: False for deterministic).
    """

    def __init__(
        self,
        workdir: Path,
        use_llm: bool = False,
    ) -> None:
        self._workdir = workdir
        self._use_llm = use_llm

    def synthesize(
        self,
        worker_results: list[dict[str, Any]],
        parent_task_id: str,
        scratchpad_path: Path | None = None,
    ) -> SynthesisResult:
        """Synthesize worker results into coherent summary.

        Args:
            worker_results: List of worker result dictionaries.
            parent_task_id: ID of the parent task.
            scratchpad_path: Optional path to shared scratchpad.

        Returns:
            SynthesisResult with merged summary.
        """
        successful = [w for w in worker_results if w.get("status") == "completed"]
        failed = [w for w in worker_results if w.get("status") == "failed"]

        # Collect all summaries
        summaries = [w.get("result_summary", "") for w in successful if w.get("result_summary")]

        # Collect all artifacts
        all_artifacts: list[str] = []
        for w in successful:
            artifacts = w.get("artifacts", [])
            all_artifacts.extend(artifacts)

        # Detect conflicts (simplified: look for contradictory statements)
        conflicts = self._detect_conflicts(summaries)

        # Generate synthesized summary
        if self._use_llm:
            synthesized = self._llm_synthesize(summaries, parent_task_id)
        else:
            synthesized = self._deterministic_synthesize(summaries, successful)

        result = SynthesisResult(
            synthesized_summary=synthesized,
            worker_count=len(worker_results),
            successful_workers=len(successful),
            failed_workers=len(failed),
            artifacts_merged=all_artifacts,
            conflicts_detected=conflicts,
        )

        logger.info(
            "Synthesized results for task %s: %d workers (%d success, %d failed)",
            parent_task_id,
            len(worker_results),
            len(successful),
            len(failed),
        )

        if conflicts:
            logger.warning(
                "Synthesis detected %d conflicts for task %s",
                len(conflicts),
                parent_task_id,
            )

        return result

    def _deterministic_synthesize(
        self,
        summaries: list[str],
        successful_workers: list[dict[str, Any]],
    ) -> str:
        """Deterministically synthesize summaries.

        Args:
            summaries: List of worker summaries.
            successful_workers: List of successful worker result dicts.

        Returns:
            Synthesized summary string.
        """
        if not summaries:
            return "No worker results to synthesize."

        # Simple deterministic merge: concatenate with worker attribution
        parts = ["## Synthesized Worker Results", ""]

        for i, (summary, worker) in enumerate(zip(summaries, successful_workers, strict=False), 1):
            worker_id = worker.get("worker_id", f"Worker-{i}")
            subtask = worker.get("subtask_id", "Unknown subtask")

            parts.append(f"### {worker_id} ({subtask})")
            parts.append("")
            parts.append(summary)
            parts.append("")

        parts.append("---")
        parts.append(f"**Total workers:** {len(successful_workers)}")

        return "\n".join(parts)

    def _llm_synthesize(
        self,
        summaries: list[str],
        parent_task_id: str,
    ) -> str:
        """Use LLM to synthesize summaries.

        Args:
            summaries: List of worker summaries.
            parent_task_id: Parent task ID for context.

        Returns:
            LLM-synthesized summary string.
        """
        # Placeholder for LLM integration
        # In production, this would call an LLM with a synthesis prompt
        logger.debug("LLM synthesis requested for task %s", parent_task_id)

        # Fall back to deterministic for now
        return self._deterministic_synthesize(summaries, [])

    def _detect_conflicts(self, summaries: list[str]) -> list[str]:
        """Detect conflicts between worker summaries.

        Args:
            summaries: List of worker summaries.

        Returns:
            List of conflict descriptions.
        """
        conflicts: list[str] = []

        # Simple conflict detection: look for contradictory keywords
        # In production, this would use more sophisticated NLP
        positive_keywords = {"success", "pass", "complete", "working"}
        negative_keywords = {"fail", "error", "broken", "issue"}

        for i, summary1 in enumerate(summaries):
            for summary2 in summaries[i + 1 :]:
                s1_lower = summary1.lower()
                s2_lower = summary2.lower()

                has_positive1 = any(kw in s1_lower for kw in positive_keywords)
                has_negative1 = any(kw in s1_lower for kw in negative_keywords)
                has_positive2 = any(kw in s2_lower for kw in positive_keywords)
                has_negative2 = any(kw in s2_lower for kw in negative_keywords)

                # Conflict: one says success, other says failure
                if (has_positive1 and has_negative2) or (has_negative1 and has_positive2):
                    conflicts.append(
                        "Conflicting results between workers: one reports success, another reports failure"
                    )

        return conflicts

    def save_synthesis_result(
        self,
        result: SynthesisResult,
        parent_task_id: str,
        output_path: Path | None = None,
    ) -> Path:
        """Save synthesis result to file.

        Args:
            result: SynthesisResult to save.
            parent_task_id: Parent task ID.
            output_path: Optional output path (default: .sdd/runtime/synthesis/).

        Returns:
            Path to saved file.
        """
        if output_path is None:
            output_dir = self._workdir / ".sdd" / "runtime" / "synthesis"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{parent_task_id}_synthesis.md"

        content = f"""# Synthesis Report for Task {parent_task_id}

## Summary

{result.synthesized_summary}

## Statistics

- **Total workers:** {result.worker_count}
- **Successful:** {result.successful_workers}
- **Failed:** {result.failed_workers}

## Artifacts

{chr(10).join(f"- `{artifact}`" for artifact in result.artifacts_merged) if result.artifacts_merged else "No artifacts"}

## Conflicts

{chr(10).join(f"- {c}" for c in result.conflicts_detected) if result.conflicts_detected else "No conflicts detected"}
"""

        output_path.write_text(content, encoding="utf-8")
        logger.info("Saved synthesis result to %s", output_path)

        return output_path


def should_synthesize(
    worker_count: int,
    coordinator_mode: bool,
    explicit_flag: bool,
) -> bool:
    """Determine if synthesis should be performed.

    Args:
        worker_count: Number of workers that executed.
        coordinator_mode: Whether coordinator mode is enabled.
        explicit_flag: Whether explicit synthesis flag is set.

    Returns:
        True if synthesis should be performed.
    """
    # Always synthesize in coordinator mode with multiple workers
    if coordinator_mode and worker_count > 1:
        return True

    # Synthesize if explicitly requested
    return bool(explicit_flag)
