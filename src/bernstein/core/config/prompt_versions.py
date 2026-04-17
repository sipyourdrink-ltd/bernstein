"""Prompt and model configuration versioning for canary deployments.

Each prompt template is hashed to create a stable version ID. The version
history is stored in .sdd/config/prompt_versions.json. Canary deployments
route a percentage of tasks to a new version while keeping the stable
version for the rest.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class PromptVersion:
    """Immutable record of a prompt/model content version."""

    version_id: str  # short hash
    prompt_hash: str  # full content hash
    role: str
    content: str
    created_at: float = field(default_factory=time.time)
    notes: str = ""


@dataclass
class CanaryState:
    """Runtime canary deployment state persisted to disk."""

    stable_version: str
    canary_version: str = ""
    canary_percentage: int = 0  # 0-100
    canary_task_count: int = 0
    stable_task_count: int = 0
    canary_pass_count: int = 0
    stable_pass_count: int = 0
    auto_promote_threshold: int = 10  # tasks before evaluation
    auto_rollback_diff_pct: float = 10.0  # % below stable triggers rollback


def hash_prompt(content: str) -> str:
    """Return the full SHA-256 hex digest of the prompt content."""
    return hashlib.sha256(content.encode()).hexdigest()


def version_id(prompt_hash: str) -> str:
    """Return the 12-char short version ID derived from a full hash."""
    return prompt_hash[:12]


def create_prompt_version(role: str, content: str, notes: str = "") -> PromptVersion:
    """Create a new PromptVersion record from role and content."""
    h = hash_prompt(content)
    return PromptVersion(
        version_id=version_id(h),
        prompt_hash=h,
        role=role,
        content=content,
        notes=notes,
    )


def load_canary_state(config_dir: Path) -> CanaryState | None:
    """Load canary state from config_dir/canary.json, or None if missing."""
    path = config_dir / "canary.json"
    if not path.exists():
        return None
    return CanaryState(**json.loads(path.read_text()))


def save_canary_state(state: CanaryState, config_dir: Path) -> None:
    """Persist canary state to config_dir/canary.json, creating the dir."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "canary.json").write_text(json.dumps(asdict(state), sort_keys=True))


def should_route_to_canary(state: CanaryState, task_index: int) -> bool:
    """Deterministic routing: every Nth task goes to canary."""
    if state.canary_percentage <= 0 or not state.canary_version:
        return False
    if state.canary_percentage >= 100:
        return True
    # Deterministic by index: task_index % 100 < percentage
    return (task_index % 100) < state.canary_percentage


def record_result(state: CanaryState, used_canary: bool, passed: bool) -> CanaryState:
    """Update pass/task counters on the state in place and return it."""
    if used_canary:
        state.canary_task_count += 1
        if passed:
            state.canary_pass_count += 1
    else:
        state.stable_task_count += 1
        if passed:
            state.stable_pass_count += 1
    return state


def evaluate_canary(state: CanaryState) -> str:
    """Return 'promote', 'rollback', or 'continue' based on pass rates."""
    if state.canary_task_count < state.auto_promote_threshold:
        return "continue"
    canary_rate = state.canary_pass_count / state.canary_task_count if state.canary_task_count else 0
    stable_rate = state.stable_pass_count / state.stable_task_count if state.stable_task_count else canary_rate
    if canary_rate >= stable_rate:
        return "promote"
    if stable_rate - canary_rate > state.auto_rollback_diff_pct / 100:
        return "rollback"
    return "continue"


def promote_canary(state: CanaryState) -> CanaryState:
    """Return a new CanaryState with the canary promoted to stable."""
    return CanaryState(
        stable_version=state.canary_version,
        canary_version="",
        canary_percentage=0,
        auto_promote_threshold=state.auto_promote_threshold,
        auto_rollback_diff_pct=state.auto_rollback_diff_pct,
    )


def rollback_canary(state: CanaryState) -> CanaryState:
    """Return a new CanaryState with the canary cleared and stable retained."""
    return CanaryState(
        stable_version=state.stable_version,
        canary_version="",
        canary_percentage=0,
        auto_promote_threshold=state.auto_promote_threshold,
        auto_rollback_diff_pct=state.auto_rollback_diff_pct,
    )
