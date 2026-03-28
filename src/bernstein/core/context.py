"""Gather project context for the manager's planning prompt.

Reads the file tree, README, and .sdd/project.md to give the LLM
enough context to decompose a goal into well-scoped tasks.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import ApiTier

_IGNORED_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".venv", "venv", "dist",
    "build", ".egg-info", ".tox", ".sdd/runtime",
})

_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".egg-info"})


def _should_skip(path: Path) -> bool:
    """Return True if *path* should be excluded from the file tree."""
    for part in path.parts:
        if part in _IGNORED_DIRS:
            return True
    return path.suffix in _IGNORED_SUFFIXES


def file_tree(workdir: Path, max_lines: int = 50) -> str:
    """Build a compact file-tree listing of the project.

    Uses ``git ls-files`` when inside a git repo (fast, respects
    .gitignore). Falls back to a recursive walk with heuristic filters.

    Args:
        workdir: Project root directory.
        max_lines: Maximum number of lines to include.

    Returns:
        A newline-separated file listing, truncated to *max_lines*.
    """
    lines: list[str] = []

    # Try git ls-files first — fast and .gitignore-aware.
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: walk the directory tree.
    if not lines:
        for path in sorted(workdir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(workdir)
            if _should_skip(rel):
                continue
            lines.append(str(rel))

    if len(lines) > max_lines:
        truncated = lines[:max_lines]
        truncated.append(f"... ({len(lines) - max_lines} more files)")
        return "\n".join(truncated)

    return "\n".join(lines)


def _read_if_exists(path: Path, max_chars: int = 4000) -> str | None:
    """Read a text file, returning None if it doesn't exist.

    Args:
        path: File to read.
        max_chars: Truncate content to this many characters.

    Returns:
        File content (possibly truncated) or None.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    return text


def available_roles(templates_dir: Path) -> list[str]:
    """Discover available specialist roles from the templates directory.

    Each subdirectory of *templates_dir* that contains a
    ``system_prompt.md`` is treated as a valid role.

    Args:
        templates_dir: Path to ``templates/roles/``.

    Returns:
        Sorted list of role names.
    """
    if not templates_dir.is_dir():
        return []
    roles: list[str] = []
    for child in sorted(templates_dir.iterdir()):
        if child.is_dir() and (child / "system_prompt.md").exists():
            roles.append(child.name)
    return roles


def gather_project_context(workdir: Path, max_lines: int = 100) -> str:
    """Gather project context for the manager: file tree, README, .sdd/project.md.

    Args:
        workdir: Project root directory.
        max_lines: Maximum file-tree lines.

    Returns:
        Formatted context string ready for prompt injection.
    """
    sections: list[str] = []

    # File tree
    tree = file_tree(workdir, max_lines=max_lines)
    if tree:
        sections.append(f"## File tree\n```\n{tree}\n```")

    # README
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = _read_if_exists(workdir / name)
        if readme:
            sections.append(f"## README\n{readme}")
            break

    # .sdd/project.md
    project_md = _read_if_exists(workdir / ".sdd" / "project.md")
    if project_md:
        sections.append(f"## Project description (.sdd/project.md)\n{project_md}")

    return "\n\n".join(sections) if sections else "(no project context available)"


# ---------------------------------------------------------------------------
# API Usage Tracking
# ---------------------------------------------------------------------------

@dataclass
class ApiCallRecord:
    """Record of a single API call.

    Attributes:
        timestamp: Unix timestamp of the call.
        provider: API provider name (e.g., "openrouter", "anthropic").
        model: Model name used (e.g., "claude-sonnet-4-20250514").
        agent_session_id: ID of the agent session that made the call.
        tokens_input: Number of input tokens.
        tokens_output: Number of output tokens.
        tokens_total: Total tokens used.
        cost_usd: Cost in USD for this call.
        latency_ms: Request latency in milliseconds.
        success: Whether the call succeeded.
        error: Error message if failed.
    """
    timestamp: float
    provider: str
    model: str
    agent_session_id: str
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    success: bool = True
    error: str | None = None


@dataclass
class ProviderUsageSummary:
    """Aggregated usage summary for a provider.

    Attributes:
        provider: Provider name.
        total_calls: Total number of API calls.
        total_tokens: Total tokens consumed.
        total_cost_usd: Total cost in USD.
        successful_calls: Number of successful calls.
        failed_calls: Number of failed calls.
        avg_latency_ms: Average latency across calls.
        models_used: Set of model names used.
    """
    provider: str
    total_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    successful_calls: int = 0
    failed_calls: int = 0
    avg_latency_ms: float = 0.0
    models_used: set[str] = field(default_factory=set)


@dataclass
class AgentSessionUsage:
    """Usage summary for an agent session.

    Attributes:
        agent_session_id: Agent session identifier.
        total_calls: Total API calls made by this session.
        total_tokens: Total tokens consumed.
        total_cost_usd: Total cost in USD.
        providers_used: Set of providers used.
        start_time: First call timestamp.
        last_activity: Last call timestamp.
    """
    agent_session_id: str
    total_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    providers_used: set[str] = field(default_factory=set)
    start_time: float | None = None
    last_activity: float | None = None


@dataclass
class TierConsumption:
    """Tier-based consumption tracking.

    Attributes:
        provider: Provider name.
        tier: API tier (free, plus, pro, etc.).
        tokens_used: Tokens consumed in this tier.
        tokens_limit: Tier token limit (if applicable).
        requests_used: Requests made in this tier.
        requests_limit: Tier request limit (if applicable).
        percentage_used: Percentage of tier quota used.
    """
    provider: str
    tier: ApiTier
    tokens_used: int = 0
    tokens_limit: int | None = None
    requests_used: int = 0
    requests_limit: int | None = None
    percentage_used: float = 0.0


class ApiUsageTracker:
    """Background service that tracks API calls, token usage, costs, and tier consumption.

    Tracks metrics per provider and per agent session, storing them in memory
    and optionally persisting to .sdd/metrics/ directory.

    Args:
        metrics_dir: Directory to store metrics files. Defaults to .sdd/metrics/.
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        self._metrics_dir = metrics_dir or Path.cwd() / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)

        # In-memory tracking
        self._calls: list[ApiCallRecord] = []
        self._provider_summaries: dict[str, ProviderUsageSummary] = {}
        self._agent_summaries: dict[str, AgentSessionUsage] = {}
        self._tier_consumption: dict[str, TierConsumption] = {}

        # EMA for latency tracking
        self._provider_latency_ema: dict[str, float] = {}

    def record_call(
        self,
        provider: str,
        model: str,
        agent_session_id: str,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        success: bool = True,
        error: str | None = None,
    ) -> ApiCallRecord:
        """Record an API call.

        Args:
            provider: API provider name.
            model: Model name used.
            agent_session_id: ID of the agent session.
            tokens_input: Input tokens count.
            tokens_output: Output tokens count.
            cost_usd: Cost in USD.
            latency_ms: Request latency.
            success: Whether the call succeeded.
            error: Error message if failed.

        Returns:
            The recorded ApiCallRecord.
        """
        tokens_total = tokens_input + tokens_output
        timestamp = time.time()

        record = ApiCallRecord(
            timestamp=timestamp,
            provider=provider,
            model=model,
            agent_session_id=agent_session_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total=tokens_total,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            success=success,
            error=error,
        )
        self._calls.append(record)
        self._update_aggregates(record)
        self._persist_record(record)

        return record

    def _update_aggregates(self, record: ApiCallRecord) -> None:
        """Update aggregated summaries with a new record.

        Args:
            record: New API call record.
        """
        # Update provider summary
        if record.provider not in self._provider_summaries:
            self._provider_summaries[record.provider] = ProviderUsageSummary(
                provider=record.provider
            )
        prov = self._provider_summaries[record.provider]
        prov.total_calls += 1
        prov.total_tokens += record.tokens_total
        prov.total_cost_usd += record.cost_usd
        if record.success:
            prov.successful_calls += 1
        else:
            prov.failed_calls += 1
        prov.models_used.add(record.model)

        # Update latency EMA
        alpha = 0.3
        if record.provider in self._provider_latency_ema:
            self._provider_latency_ema[record.provider] = (
                alpha * record.latency_ms +
                (1 - alpha) * self._provider_latency_ema[record.provider]
            )
        else:
            self._provider_latency_ema[record.provider] = record.latency_ms
        prov.avg_latency_ms = self._provider_latency_ema[record.provider]

        # Update agent session summary
        if record.agent_session_id not in self._agent_summaries:
            self._agent_summaries[record.agent_session_id] = AgentSessionUsage(
                agent_session_id=record.agent_session_id
            )
        agent = self._agent_summaries[record.agent_session_id]
        agent.total_calls += 1
        agent.total_tokens += record.tokens_total
        agent.total_cost_usd += record.cost_usd
        agent.providers_used.add(record.provider)
        if agent.start_time is None:
            agent.start_time = record.timestamp
        agent.last_activity = record.timestamp

    def _persist_record(self, record: ApiCallRecord) -> None:
        """Persist a record to the metrics directory.

        Args:
            record: API call record to persist.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"api_calls_{today}.jsonl"
        filepath = self._metrics_dir / filename

        data = {
            "timestamp": record.timestamp,
            "provider": record.provider,
            "model": record.model,
            "agent_session_id": record.agent_session_id,
            "tokens_input": record.tokens_input,
            "tokens_output": record.tokens_output,
            "tokens_total": record.tokens_total,
            "cost_usd": record.cost_usd,
            "latency_ms": record.latency_ms,
            "success": record.success,
            "error": record.error,
        }

        with filepath.open("a") as f:
            f.write(json.dumps(data) + "\n")

    def set_tier_consumption(
        self,
        provider: str,
        tier: ApiTier,
        tokens_used: int = 0,
        tokens_limit: int | None = None,
        requests_used: int = 0,
        requests_limit: int | None = None,
    ) -> None:
        """Set or update tier consumption for a provider.

        Args:
            provider: Provider name.
            tier: API tier.
            tokens_used: Tokens consumed.
            tokens_limit: Token limit for tier.
            requests_used: Requests made.
            requests_limit: Request limit for tier.
        """
        key = f"{provider}:{tier.value}"
        percentage = 0.0
        if tokens_limit:
            percentage = max(percentage, tokens_used / tokens_limit * 100)
        if requests_limit:
            percentage = max(percentage, requests_used / requests_limit * 100)

        self._tier_consumption[key] = TierConsumption(
            provider=provider,
            tier=tier,
            tokens_used=tokens_used,
            tokens_limit=tokens_limit,
            requests_used=requests_used,
            requests_limit=requests_limit,
            percentage_used=percentage,
        )

    def get_provider_summary(self, provider: str) -> ProviderUsageSummary | None:
        """Get usage summary for a specific provider.

        Args:
            provider: Provider name.

        Returns:
            ProviderUsageSummary or None if not found.
        """
        return self._provider_summaries.get(provider)

    def get_agent_summary(self, agent_session_id: str) -> AgentSessionUsage | None:
        """Get usage summary for a specific agent session.

        Args:
            agent_session_id: Agent session ID.

        Returns:
            AgentSessionUsage or None if not found.
        """
        return self._agent_summaries.get(agent_session_id)

    def get_all_provider_summaries(self) -> dict[str, ProviderUsageSummary]:
        """Get all provider usage summaries.

        Returns:
            Dict of provider name to ProviderUsageSummary.
        """
        return dict(self._provider_summaries)

    def get_all_agent_summaries(self) -> dict[str, AgentSessionUsage]:
        """Get all agent session usage summaries.

        Returns:
            Dict of agent session ID to AgentSessionUsage.
        """
        return dict(self._agent_summaries)

    def get_tier_consumption(self, provider: str) -> list[TierConsumption]:
        """Get tier consumption for a provider.

        Args:
            provider: Provider name.

        Returns:
            List of TierConsumption for all tiers.
        """
        return [
            tc for key, tc in self._tier_consumption.items()
            if tc.provider == provider
        ]

    def get_global_summary(self) -> dict[str, str]:
        """Get a global summary of all API usage.

        Returns:
            Dict with aggregated metrics as string values for endpoint exposure.
        """
        total_calls = sum(p.total_calls for p in self._provider_summaries.values())
        total_tokens = sum(p.total_tokens for p in self._provider_summaries.values())
        total_cost = sum(p.total_cost_usd for p in self._provider_summaries.values())
        total_success = sum(p.successful_calls for p in self._provider_summaries.values())
        total_failed = sum(p.failed_calls for p in self._provider_summaries.values())

        return {
            "total_api_calls": str(total_calls),
            "total_tokens_consumed": str(total_tokens),
            "total_cost_usd": f"{total_cost:.4f}",
            "successful_calls": str(total_success),
            "failed_calls": str(total_failed),
            "success_rate": f"{total_success / total_calls:.2%}" if total_calls > 0 else "N/A",
            "providers_active": str(len(self._provider_summaries)),
            "agent_sessions_active": str(len(self._agent_summaries)),
        }

    def get_summary_for_agent(self, agent_session_id: str) -> dict[str, str]:
        """Get usage summary for a specific agent session.

        Args:
            agent_session_id: Agent session ID.

        Returns:
            Dict with metrics as string values.
        """
        agent = self._agent_summaries.get(agent_session_id)
        if not agent:
            return {"error": "Agent session not found"}

        return {
            "agent_session_id": agent.agent_session_id,
            "total_calls": str(agent.total_calls),
            "total_tokens": str(agent.total_tokens),
            "total_cost_usd": f"{agent.total_cost_usd:.4f}",
            "providers_used": ", ".join(sorted(agent.providers_used)),
            "start_time": datetime.fromtimestamp(agent.start_time).isoformat() if agent.start_time else "N/A",
            "last_activity": datetime.fromtimestamp(agent.last_activity).isoformat() if agent.last_activity else "N/A",
        }

    def export_summary(self, output_path: Path) -> None:
        """Export full usage summary to a JSON file.

        Args:
            output_path: Path to write the export.
        """
        data = {
            "exported_at": datetime.now().isoformat(),
            "global_summary": self.get_global_summary(),
            "provider_summaries": {
                name: {
                    "provider": s.provider,
                    "total_calls": s.total_calls,
                    "total_tokens": s.total_tokens,
                    "total_cost_usd": round(s.total_cost_usd, 4),
                    "successful_calls": s.successful_calls,
                    "failed_calls": s.failed_calls,
                    "avg_latency_ms": round(s.avg_latency_ms, 2),
                    "models_used": sorted(s.models_used),
                }
                for name, s in self._provider_summaries.items()
            },
            "agent_summaries": {
                aid: {
                    "agent_session_id": s.agent_session_id,
                    "total_calls": s.total_calls,
                    "total_tokens": s.total_tokens,
                    "total_cost_usd": round(s.total_cost_usd, 4),
                    "providers_used": sorted(s.providers_used),
                }
                for aid, s in self._agent_summaries.items()
            },
            "tier_consumption": {
                key: {
                    "provider": tc.provider,
                    "tier": tc.tier.value,
                    "tokens_used": tc.tokens_used,
                    "tokens_limit": tc.tokens_limit,
                    "requests_used": tc.requests_used,
                    "requests_limit": tc.requests_limit,
                    "percentage_used": round(tc.percentage_used, 2),
                }
                for key, tc in self._tier_consumption.items()
            },
        }

        with output_path.open("w") as f:
            json.dump(data, f, indent=2)


# Global instance for easy access
_default_usage_tracker: ApiUsageTracker | None = None


def get_usage_tracker(metrics_dir: Path | None = None) -> ApiUsageTracker:
    """Get or create the default API usage tracker.

    Args:
        metrics_dir: Optional custom metrics directory.

    Returns:
        ApiUsageTracker instance.
    """
    global _default_usage_tracker
    if _default_usage_tracker is None:
        _default_usage_tracker = ApiUsageTracker(metrics_dir)
    return _default_usage_tracker
