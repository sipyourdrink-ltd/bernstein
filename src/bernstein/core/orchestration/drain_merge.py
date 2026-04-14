"""Spawn an Opus-level Claude Code agent to cherry-pick completed agent work into main.

After a graceful drain, agents leave commits on ``agent/*`` branches in git
worktrees.  This module builds a targeted prompt, spawns ``claude`` as a
subprocess, and parses the structured JSON report it produces.  The merge
agent evaluates each branch, cherry-picks clean work, and skips anything
broken or conflicting.

The module is intentionally defensive: every failure path returns an empty
result list and logs a warning so the drain coordinator can proceed to
cleanup without crashing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

_MERGE_REPORT_SENTINEL = "MERGE_REPORT_JSON:"


@dataclass(frozen=True)
class MergeResult:
    """Outcome of evaluating a single agent branch for merge into main."""

    branch: str
    action: str  # "merged" or "skipped"
    files_changed: int
    reason: str


def _build_prompt(branches: list[str]) -> str:
    """Build the merge-agent system prompt.

    Args:
        branches: Branch names with completed work to evaluate.

    Returns:
        Full prompt string for the Claude merge agent.
    """
    branch_list = "\n".join(f"  - {b}" for b in branches)
    return (
        "You are performing a post-drain merge for Bernstein orchestrator.\n"
        "Your working directory is the main branch of the project.\n"
        "\n"
        "Branches with completed work to evaluate:\n"
        f"{branch_list}\n"
        "\n"
        "For EACH branch:\n"
        "1. Run: git log main..{branch} --oneline\n"
        "2. Run: git diff main..{branch} --stat\n"
        "3. Decide: CHERRY-PICK (clean, complete work) or SKIP (broken/partial/conflicts)\n"
        "4. For CHERRY-PICK: run `git cherry-pick {commit}` for each commit on the branch\n"
        "5. After cherry-pick: run `uv run ruff check src/` -- if it fails, "
        "run `git cherry-pick --abort` and SKIP\n"
        "6. Then run `uv run python scripts/run_tests.py -x` -- if tests fail, "
        "run `git cherry-pick --abort` and SKIP\n"
        "7. For SKIP: note the reason\n"
        "\n"
        "After processing ALL branches, output EXACTLY this JSON (no other text after it):\n"
        f"{_MERGE_REPORT_SENTINEL}\n"
        '[{"branch": "agent/backend-abc123", "action": "merged", '
        '"files_changed": 3, "reason": "clean cherry-pick"}, ...]\n'
        "\n"
        "Rules:\n"
        "- Cherry-pick individual commits, not merge\n"
        "- If cherry-pick conflicts and you can resolve trivially, do so\n"
        "- If conflicts are complex, abort and SKIP\n"
        "- Never cherry-pick commits that would break the build\n"
        "- The JSON must be valid and parseable\n"
    )


def _parse_report(stdout: str) -> list[MergeResult]:
    """Extract and parse the JSON merge report from agent stdout.

    Args:
        stdout: Full captured stdout from the Claude process.

    Returns:
        Parsed list of ``MergeResult`` objects, or an empty list on failure.
    """
    idx = stdout.rfind(_MERGE_REPORT_SENTINEL)
    if idx == -1:
        logger.warning("Merge agent output did not contain %s", _MERGE_REPORT_SENTINEL)
        return []

    json_text = stdout[idx + len(_MERGE_REPORT_SENTINEL) :].strip()

    # The agent may emit trailing text after the JSON array.  Find the
    # closing bracket so we can parse just the array.
    bracket_start = json_text.find("[")
    if bracket_start == -1:
        logger.warning("No JSON array found after sentinel")
        return []

    depth = 0
    bracket_end = -1
    for i in range(bracket_start, len(json_text)):
        ch = json_text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                bracket_end = i
                break

    if bracket_end == -1:
        logger.warning("Unterminated JSON array in merge report")
        return []

    raw_array = json_text[bracket_start : bracket_end + 1]

    try:
        items: list[dict[str, object]] = json.loads(raw_array)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse merge report JSON: %s", exc)
        return []

    results: list[MergeResult] = []
    for item in items:
        try:
            results.append(
                MergeResult(
                    branch=str(item.get("branch", "")),
                    action=str(item.get("action", "skipped")),
                    files_changed=int(item.get("files_changed", 0)),  # type: ignore[arg-type]
                    reason=str(item.get("reason", "")),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping malformed merge-report entry: %s", exc)
    return results


async def run_merge_agent(
    branches: list[str],
    workdir: Path,
    *,
    model: str = "opus",
    effort: str = "max",
    timeout_s: int = 120,
) -> list[MergeResult]:
    """Spawn a Claude Code agent to cherry-pick completed branch work into main.

    The agent evaluates each branch in *branches*, cherry-picks clean work,
    and skips anything broken or conflicting.  Results are parsed from a
    structured JSON report emitted by the agent.

    Args:
        branches: Git branch names (e.g. ``agent/backend-abc123``) to evaluate.
        workdir: Project root directory checked out on the main branch.
        model: Short model name (``opus``, ``sonnet``, ``haiku``) or a full
            model identifier.
        effort: Claude effort level (``max``, ``high``, ``medium``, ``low``).
        timeout_s: Maximum seconds to wait for the merge agent.

    Returns:
        A list of :class:`MergeResult` describing each branch outcome.
        Returns an empty list when the agent fails, times out, or produces
        unparseable output.
    """
    if not branches:
        logger.info("No branches to merge -- skipping merge agent")
        return []

    prompt = _build_prompt(branches)
    model_id = _MODEL_MAP.get(model, model)

    cmd: list[str] = [
        "claude",
        "--model",
        model_id,
        "--effort",
        effort,
        "--dangerously-skip-permissions",
        "--max-turns",
        "50",
        "--output-format",
        "text",
        "--verbose",
        "-p",
        prompt,
    ]

    env = os.environ.copy()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
        )
    except FileNotFoundError:
        logger.warning("claude CLI not found in PATH -- cannot run merge agent")
        return []
    except OSError as exc:
        logger.warning("Failed to spawn merge agent: %s", exc)
        return []

    try:
        async with asyncio.timeout(timeout_s):
            stdout_bytes, _ = await proc.communicate()
    except TimeoutError:
        logger.warning("Merge agent timed out after %ds -- killing process", timeout_s)
        with contextlib.suppress(OSError):
            proc.kill()
        # Drain remaining output so the transport closes cleanly.
        with contextlib.suppress(Exception):
            await proc.communicate()
        return []

    if proc.returncode != 0:
        logger.warning("Merge agent exited with code %s", proc.returncode)
        return []

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    results = _parse_report(stdout)

    if results:
        merged = sum(1 for r in results if r.action == "merged")
        skipped = sum(1 for r in results if r.action == "skipped")
        logger.info("Merge agent finished: %d merged, %d skipped", merged, skipped)
    else:
        logger.warning("Merge agent produced no parseable results")

    return results
