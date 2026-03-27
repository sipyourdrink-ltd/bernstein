"""Auto-discover installed CLI coding agents, check login status, and register capabilities.

Scans the system PATH for known CLI agent binaries, probes their login/auth
state, and returns a structured description of what each agent can do. Used
by ``bernstein doctor``, ``bernstein init``, and the auto-routing layer.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Maximum time (seconds) for any single subprocess probe.
_PROBE_TIMEOUT_S: Final[float] = 3.0

# Cache TTL — avoid re-scanning within the same session.
_CACHE_TTL_S: Final[float] = 300.0  # 5 minutes


@dataclass(frozen=True)
class AgentCapabilities:
    """What a discovered CLI agent can do."""

    name: str  # e.g. "codex", "gemini", "claude"
    binary: str  # path to binary
    version: str  # e.g. "1.2.3"
    logged_in: bool  # is the user authenticated?
    login_method: str  # e.g. "ChatGPT", "API key", "gcloud", ""
    available_models: list[str]  # models this agent can use
    default_model: str  # default model
    supports_headless: bool  # can run non-interactively
    supports_sandbox: bool  # has sandbox mode
    supports_mcp: bool  # can use MCP servers
    max_context_tokens: int  # approximate context window
    reasoning_strength: str  # "low", "medium", "high", "very_high"
    best_for: list[str]  # e.g. ["frontend", "fast-tasks", "code-review"]
    cost_tier: str  # "free", "cheap", "moderate", "expensive"


@dataclass
class DiscoveryResult:
    """Result of scanning for available agents."""

    agents: list[AgentCapabilities]
    warnings: list[str] = field(default_factory=list[str])
    scan_time_ms: float = 0.0


# ---------------------------------------------------------------------------
# Internal probe helpers
# ---------------------------------------------------------------------------


def _run_probe(cmd: list[str], timeout: float = _PROBE_TIMEOUT_S) -> subprocess.CompletedProcess[str] | None:
    """Run a subprocess probe with a short timeout.

    Returns None on any error (FileNotFoundError, timeout, permission, etc.).
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError, OSError):
        return None


def _extract_version(result: subprocess.CompletedProcess[str] | None) -> str:
    """Best-effort version extraction from --version output."""
    if result is None or result.returncode != 0:
        return "unknown"
    text = (result.stdout + result.stderr).strip()
    # Many CLIs print "name vX.Y.Z" or just "X.Y.Z"
    for token in text.split():
        stripped = token.lstrip("v").strip(",").strip("(").strip(")")
        if stripped and stripped[0].isdigit():
            return stripped
    return text[:40] if text else "unknown"


# ---------------------------------------------------------------------------
# Per-agent detection
# ---------------------------------------------------------------------------


def _detect_codex() -> tuple[AgentCapabilities | None, list[str]]:
    """Detect OpenAI Codex CLI."""
    warnings: list[str] = []
    binary = shutil.which("codex")
    if binary is None:
        return None, []

    # Version
    version = _extract_version(_run_probe(["codex", "--version"]))

    # Login check: `codex login status`
    logged_in = False
    login_method = ""
    login_result = _run_probe(["codex", "login", "status"])
    if login_result is not None:
        combined = login_result.stdout + login_result.stderr
        combined_lower = combined.lower()
        # "Not logged in" also contains "logged in", so check returncode
        # and exclude explicit "not logged in" matches.
        is_positive = (
            "logged in" in combined_lower and "not logged in" not in combined_lower and login_result.returncode == 0
        )
        if is_positive:
            logged_in = True
            if "chatgpt" in combined_lower:
                login_method = "ChatGPT"
            elif "api" in combined_lower:
                login_method = "API key"
            else:
                login_method = "CLI auth"
    # Also accept OPENAI_API_KEY as auth
    if not logged_in and os.environ.get("OPENAI_API_KEY"):
        logged_in = True
        login_method = "API key"

    if binary and not logged_in:
        warnings.append("codex found but not logged in — run: codex login")

    return AgentCapabilities(
        name="codex",
        binary=binary,
        version=version,
        logged_in=logged_in,
        login_method=login_method,
        available_models=["gpt-5.4", "gpt-5.4-mini", "o3", "o4-mini"],
        default_model="gpt-5.4",
        supports_headless=True,
        supports_sandbox=True,
        supports_mcp=True,
        max_context_tokens=200_000,
        reasoning_strength="high",
        best_for=["quick-fixes", "code-review", "test-writing", "reasoning-tasks"],
        cost_tier="cheap",  # o4-mini $1.10/$4.40 per 1M; o3 $2/$8 per 1M
    ), warnings


def _detect_gemini() -> tuple[AgentCapabilities | None, list[str]]:
    """Detect Google Gemini CLI."""
    warnings: list[str] = []
    binary = shutil.which("gemini")
    if binary is None:
        return None, []

    # Version
    version = _extract_version(_run_probe(["gemini", "--version"]))

    # Login check: use shared detection from preflight
    from bernstein.core.preflight import gemini_has_auth

    logged_in, login_method = gemini_has_auth()

    if binary and not logged_in:
        warnings.append(
            "gemini found but not logged in — set GOOGLE_API_KEY, GEMINI_API_KEY, or run: gcloud auth login"
        )

    return AgentCapabilities(
        name="gemini",
        binary=binary,
        version=version,
        logged_in=logged_in,
        login_method=login_method,
        available_models=["gemini-3-pro", "gemini-3-flash", "gemini-2.5-pro"],
        default_model="gemini-3-pro",
        supports_headless=True,
        supports_sandbox=True,
        supports_mcp=True,
        max_context_tokens=1_000_000,
        reasoning_strength="very_high",
        best_for=["frontend", "long-context", "multimodal", "free-tier"],
        cost_tier="free",  # generous free tier; paid: 3-pro ~$2-4/$12-18 per 1M
    ), warnings


def _detect_claude() -> tuple[AgentCapabilities | None, list[str]]:
    """Detect Claude Code CLI."""
    warnings: list[str] = []
    binary = shutil.which("claude")
    if binary is None:
        return None, []

    # Version
    version = _extract_version(_run_probe(["claude", "--version"]))

    # Login check: API key or OAuth session
    logged_in = False
    login_method = ""
    if os.environ.get("ANTHROPIC_API_KEY"):
        logged_in = True
        login_method = "API key"
    else:
        # Check for OAuth session — claude --version succeeding is a good proxy
        oauth_probe = _run_probe(["claude", "--version"])
        if oauth_probe is not None and oauth_probe.returncode == 0:
            # Claude Code binary exists and is functional; OAuth may be active
            # but we can't fully confirm without an actual API call.
            # Check for OAuth credential files.
            claude_dir = Path.home() / ".claude"
            if claude_dir.exists():
                logged_in = True
                login_method = "OAuth"

    if binary and not logged_in:
        warnings.append("claude found but not authenticated — set ANTHROPIC_API_KEY or run: claude login")

    return AgentCapabilities(
        name="claude",
        binary=binary,
        version=version,
        logged_in=logged_in,
        login_method=login_method,
        available_models=["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001"],
        default_model="claude-sonnet-4-6",
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=True,
        max_context_tokens=200_000,  # 1M with extended context on Opus/Sonnet 4.6
        reasoning_strength="very_high",
        best_for=["architecture", "complex-refactoring", "security-review", "tool-use"],
        cost_tier="moderate",  # Opus $5/$25, Sonnet $3/$15, Haiku $1/$5 per 1M
        # SWE-bench Verified: Opus 80.8%, Sonnet 79.6%
    ), warnings


def _detect_qwen() -> tuple[AgentCapabilities | None, list[str]]:
    """Detect Qwen Code CLI."""
    warnings: list[str] = []
    binary = shutil.which("qwen-code") or shutil.which("qwen")
    if binary is None:
        return None, []

    # Version
    binary_name = Path(binary).name
    version = _extract_version(_run_probe([binary_name, "--version"]))

    # Login check: any of the supported API keys
    logged_in = False
    login_method = ""
    key_vars = [
        ("OPENROUTER_API_KEY_PAID", "OpenRouter"),
        ("OPENROUTER_API_KEY_FREE", "OpenRouter (free)"),
        ("OPENAI_API_KEY", "OpenAI"),
        ("TOGETHERAI_USER_KEY", "Together.ai"),
    ]
    for var, method in key_vars:
        if os.environ.get(var):
            logged_in = True
            login_method = method
            break

    if binary and not logged_in:
        warnings.append("qwen found but no API key set — set OPENROUTER_API_KEY_PAID or OPENAI_API_KEY")

    return AgentCapabilities(
        name="qwen",
        binary=binary,
        version=version,
        logged_in=logged_in,
        login_method=login_method,
        available_models=["qwen-max", "qwen-plus", "qwen-turbo"],
        default_model="qwen-max",
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=False,
        max_context_tokens=128_000,
        reasoning_strength="medium",
        best_for=["code-generation", "translation"],
        cost_tier="cheap",
    ), warnings


def _detect_aider() -> tuple[AgentCapabilities | None, list[str]]:
    """Detect Aider CLI."""
    warnings: list[str] = []
    binary = shutil.which("aider")
    if binary is None:
        return None, []

    # Version
    version = _extract_version(_run_probe(["aider", "--version"]))

    # Login check: aider --version working is sufficient as auth indicator
    # Aider can work with local models or via API keys (OpenAI, etc.)
    logged_in = False
    login_method = ""
    if os.environ.get("OPENAI_API_KEY"):
        logged_in = True
        login_method = "API key"
    elif _run_probe(["aider", "--version"]) is not None:
        # If aider --version works, it's at least installed and functional
        logged_in = True
        login_method = "local"

    if binary and not logged_in:
        warnings.append("aider found but not authenticated — set OPENAI_API_KEY or configure local model")

    return AgentCapabilities(
        name="aider",
        binary=binary,
        version=version,
        logged_in=logged_in,
        login_method=login_method,
        available_models=["gpt-4", "gpt-3.5-turbo", "claude-3-opus", "local"],
        default_model="gpt-4",
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=False,
        max_context_tokens=128_000,
        reasoning_strength="medium",
        best_for=["interactive-editing", "code-modification"],
        cost_tier="cheap",
    ), warnings


# ---------------------------------------------------------------------------
# Main discovery entry point
# ---------------------------------------------------------------------------

# All detectors, executed in order.
_DETECTORS: list[tuple[str, type[None]]] = []  # unused, kept for potential plugin registration


def discover_agents() -> DiscoveryResult:
    """Scan system for all available CLI coding agents.

    Probes each known CLI binary (codex, gemini, claude, qwen, aider), checks login
    status, and returns structured capabilities.  The entire scan targets < 2 s
    wall-clock time by using short subprocess timeouts.

    Returns:
        DiscoveryResult with discovered agents and any warnings.
    """
    start = time.monotonic()
    agents: list[AgentCapabilities] = []
    warnings: list[str] = []

    for detector in (_detect_claude, _detect_codex, _detect_gemini, _detect_qwen, _detect_aider):
        try:
            agent, agent_warnings = detector()
            if agent is not None:
                agents.append(agent)
            warnings.extend(agent_warnings)
        except Exception:
            name = getattr(detector, "__name__", repr(detector))
            logger.warning("Agent detection failed for %s", name, exc_info=True)

    elapsed_ms = (time.monotonic() - start) * 1000
    return DiscoveryResult(agents=agents, warnings=warnings, scan_time_ms=elapsed_ms)


# ---------------------------------------------------------------------------
# Session-level cache
# ---------------------------------------------------------------------------

_cached_result: DiscoveryResult | None = None
_cached_at: float = 0.0


def discover_agents_cached() -> DiscoveryResult:
    """Return cached discovery result, re-scanning if TTL has expired."""
    global _cached_result, _cached_at
    now = time.monotonic()
    if _cached_result is not None and (now - _cached_at) < _CACHE_TTL_S:
        return _cached_result
    _cached_result = discover_agents()
    _cached_at = now
    return _cached_result


def clear_discovery_cache() -> None:
    """Force the next ``discover_agents_cached`` call to re-scan."""
    global _cached_result, _cached_at
    _cached_result = None
    _cached_at = 0.0


def detect_auth_status() -> dict[str, tuple[bool, bool]]:
    """Detect installation and authentication status for all agents.

    Scans the system for installed CLI coding agents and checks their
    authentication status.

    Returns:
        A dictionary mapping agent name to (installed, authenticated) tuple.
        - installed: True if the CLI binary is found on PATH
        - authenticated: True if the agent has valid credentials/auth configured

    Example:
        {
            "claude": (True, True),     # installed and authenticated
            "codex": (True, False),     # installed but not authenticated
            "gemini": (False, False),   # not installed
            "aider": (True, True),      # installed and authenticated
        }
    """
    discovery = discover_agents_cached()

    # All known agents to report on, even if not found
    all_agents = {"claude", "codex", "gemini", "qwen", "aider"}

    result: dict[str, tuple[bool, bool]] = {}

    # Populate found agents
    for agent in discovery.agents:
        result[agent.name] = (True, agent.logged_in)

    # Add missing agents as not installed
    found_agents = {agent.name for agent in discovery.agents}
    for agent_name in all_agents - found_agents:
        result[agent_name] = (False, False)

    return result


# ---------------------------------------------------------------------------
# Role-to-agent routing recommendation
# ---------------------------------------------------------------------------

# Default role preferences — maps role to a prioritized list of
# (agent_name, model) tuples. The first available match wins.
#
# Rationale (2026-03-28 benchmark data):
# - Claude Opus 4.6: SWE-bench 80.8%, best tool-use, best for architecture/security
# - Claude Sonnet 4.6: SWE-bench 79.6%, best speed/quality ratio for implementation
# - Codex o3: SWE-bench ~78%, strong chain-of-thought reasoning
# - Codex o4-mini: SWE-bench ~72%, cheap+fast, good for focused tasks
# - Gemini 2.5-pro: SWE-bench ~76%, 1M context, free tier (1000 req/day)
# - Gemini 2.5-flash: fast, free tier, good for UI/docs/simple tasks
_ROLE_PREFERENCES: dict[str, list[tuple[str, str]]] = {
    "manager": [("claude", "claude-opus-4-6"), ("codex", "o3"), ("gemini", "gemini-2.5-pro")],
    "architect": [("claude", "claude-opus-4-6"), ("codex", "o3"), ("gemini", "gemini-2.5-pro")],
    "backend": [("claude", "claude-sonnet-4-6"), ("codex", "o4-mini"), ("gemini", "gemini-2.5-flash")],
    "frontend": [("gemini", "gemini-2.5-flash"), ("claude", "claude-sonnet-4-6"), ("codex", "o4-mini")],
    "qa": [("codex", "o4-mini"), ("gemini", "gemini-2.5-flash"), ("claude", "claude-sonnet-4-6")],
    "security": [("claude", "claude-opus-4-6"), ("codex", "o3"), ("gemini", "gemini-2.5-pro")],
    "docs": [("gemini", "gemini-2.5-flash"), ("claude", "claude-haiku-4-5-20251001"), ("codex", "o4-mini")],
    "devops": [("codex", "o4-mini"), ("claude", "claude-sonnet-4-6"), ("gemini", "gemini-2.5-flash")],
    "resolver": [("gemini", "gemini-2.5-flash"), ("codex", "o4-mini"), ("claude", "claude-haiku-4-5-20251001")],
}


@dataclass(frozen=True)
class RouteRecommendation:
    """Recommended agent + model for a specific role."""

    role: str
    agent_name: str
    model: str
    reason: str


def recommend_routing(discovery: DiscoveryResult | None = None) -> list[RouteRecommendation]:
    """Generate routing recommendations based on discovered agents.

    For each known role, picks the best available agent+model combination
    based on hardcoded preferences.

    Args:
        discovery: Pre-computed discovery result. If None, uses cached scan.

    Returns:
        List of recommendations, one per role (only for roles with a viable agent).
    """
    if discovery is None:
        discovery = discover_agents_cached()

    # Build set of available, logged-in agent names
    available = {a.name for a in discovery.agents if a.logged_in}

    recommendations: list[RouteRecommendation] = []
    for role, prefs in _ROLE_PREFERENCES.items():
        for agent_name, model in prefs:
            if agent_name in available:
                # Find the agent to pull reasoning info
                agent = next(a for a in discovery.agents if a.name == agent_name)
                reason = _build_reason(agent, role)
                recommendations.append(
                    RouteRecommendation(
                        role=role,
                        agent_name=agent_name,
                        model=model,
                        reason=reason,
                    )
                )
                break
    return recommendations


def _build_reason(agent: AgentCapabilities, role: str) -> str:
    """Build a human-readable reason for recommending an agent for a role."""
    parts: list[str] = []
    if role in ("architect", "security", "manager") and agent.reasoning_strength == "very_high":
        parts.append("strongest reasoning")
    elif role in ("qa", "docs") and agent.cost_tier in ("free", "cheap"):
        parts.append("cheap" if agent.cost_tier == "cheap" else "free tier")
    elif role == "frontend" and "frontend" in agent.best_for:
        parts.append("good at UI")
    elif role == "backend" and agent.cost_tier in ("free", "cheap"):
        parts.append("fast, cheap")
    if agent.cost_tier == "free":
        parts.append("free tier")
    if not parts:
        parts.append("best available")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# YAML generation for auto-detected agents
# ---------------------------------------------------------------------------


def generate_auto_routing_yaml(discovery: DiscoveryResult | None = None) -> str:
    """Generate a routing YAML snippet based on discovered agents.

    Produces a ``routing:`` block mapping roles to ``agent-model`` strings.

    Args:
        discovery: Pre-computed discovery result. If None, uses cached scan.

    Returns:
        YAML string suitable for inclusion in bernstein.yaml.
    """
    recs = recommend_routing(discovery)
    if not recs:
        return ""

    agent_names = sorted({r.agent_name for r in recs})
    lines = [
        f"# Auto-detected agents: {', '.join(agent_names)}",
        "cli: auto  # Bernstein picks the best agent per task",
        "",
        "routing:",
    ]
    for rec in recs:
        # Produce short model aliases
        model_alias: str = short_model(rec.model)
        lines.append(f"  {rec.role}: {rec.agent_name}-{model_alias}     # {rec.reason}")
    return "\n".join(lines) + "\n"


def short_model(model: str) -> str:
    """Convert full model ID to a short display name."""
    mapping: dict[str, str] = {
        "claude-opus-4-6": "opus",
        "claude-sonnet-4-6": "sonnet",
        "claude-haiku-4-5": "haiku",
        "claude-haiku-4-5-20251001": "haiku",
        "gemini-2.5-pro": "2.5-pro",
        "gemini-2.5-flash": "2.5-flash",
        "gemini-2.0-flash": "2.0-flash",
        "o4-mini": "o4-mini",
        "o3": "o3",
        "codex-mini": "codex-mini",
        "qwen-max": "max",
        "qwen-plus": "plus",
        "qwen-turbo": "turbo",
    }
    return mapping.get(model, model)
