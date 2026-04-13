"""AgencyProvider — loads CatalogAgent instances from msitarzewski/agency-agents format.

Agency repos use one Markdown file per agent, organised into division
subdirectories. Each file has YAML frontmatter (name, description, …)
followed by the system-prompt body.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from bernstein.agents.catalog import CatalogAgent

logger = logging.getLogger(__name__)

# Maps Agency division names (or their base component) to Bernstein role names.
_DIVISION_ROLE_MAP: dict[str, str] = {
    "engineering": "backend",
    "design": "architect",
    "testing": "qa",
    "product": "manager",
    "project-management": "manager",
    "specialized": "backend",
}

# Divisions that are clearly NOT software engineering — agents from these
# divisions are skipped entirely to avoid polluting the catalog with
# irrelevant matches (e.g. "Brand Guardian" for an architect role).
_NON_SOFTWARE_DIVISIONS: frozenset[str] = frozenset(
    {
        "marketing",
        "sales",
        "support",
        "paid-media",
        "strategy",
        "academic",
        "spatial-computing",
        "game-development",
        "integrations",
        "examples",
    }
)

# Keyword signals for each Bernstein role.  Each entry is a list of
# (phrase, weight) pairs scored against the agent's NAME only (not the full
# description, which contains too many false-positive keywords like
# "security guardrails" on a non-security agent).
#
# Multi-word phrases get higher weight to prevent false positives.
_ROLE_SIGNALS_NAME: dict[str, list[tuple[str, int]]] = {
    "reviewer": [
        ("code review", 10),
        ("reviewer", 8),
    ],
    "qa": [
        ("test", 6),
        ("tester", 8),
        ("qa ", 8),
        ("quality", 6),
        ("auditor", 4),
    ],
    "security": [
        ("security", 10),
        ("threat", 8),
        ("blockchain security", 10),
    ],
    "devops": [
        ("devops", 10),
        ("automator", 6),
        ("sre", 8),
        ("site reliability", 10),
        ("orchestrator", 4),
        ("data engineer", 8),
        ("data consolidation", 6),
    ],
    "frontend": [
        ("frontend", 10),
        ("front-end", 10),
        ("ui ", 8),
        ("ux ", 8),
        ("ui designer", 10),
        ("ux architect", 10),
        ("ux researcher", 8),
    ],
    "backend": [
        ("backend", 10),
        ("back-end", 10),
        ("api ", 6),
        ("database", 6),
    ],
    "architect": [
        ("software architect", 12),
        ("system architect", 12),
        ("architect", 6),
    ],
    "docs": [
        ("technical writ", 10),
        ("documentation", 8),
    ],
    "manager": [
        ("project manage", 10),
        ("product manage", 10),
        ("project shepherd", 8),
        ("sprint", 6),
    ],
    "ml-engineer": [
        ("machine learning", 10),
        ("ml ", 8),
        ("ai engineer", 10),
        ("deep learning", 10),
        ("data scien", 8),
    ],
}

# Phrases in the agent NAME that indicate it is NOT a software engineering
# agent.  If any phrase matches, the agent is skipped during loading.
_NON_SOFTWARE_NAME_SIGNALS: list[str] = [
    "brand",
    "marketing",
    "sales",
    "recruitment",
    "consulting",
    "presales",
    "training designer",
    "supply chain",
    "study abroad",
    "cultural intelligence",
    "korean business",
    "french consulting",
    "nudge engine",
    "trend research",
    "whimsy",
    "visual storytell",
    "image prompt",
    "inclusive visuals",
    "feedback synthesizer",
    "experiment tracker",
    "healthcare marketing",
    "studio operations",
    "studio producer",
    "report distribution",
    "identity graph",
    "accounts payable",
    "sales data",
    "filament",
]

_DEFAULT_AGENCY_SOURCE = "https://github.com/msitarzewski/agency-agents"
_SYNC_TTL_SECONDS = 86400  # 24 hours


def _slugify(name: str) -> str:
    """Return a URL-safe slug for *name*."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _division_to_role(division: str) -> str:
    """Map an Agency division name to a Bernstein role string.

    Checks the full division name first, then the base component (part before
    the first ``_`` or ``-``).

    Args:
        division: Subdirectory name from the Agency repo (e.g. ``"engineering"``,
            ``"qa_testing"``).

    Returns:
        Bernstein role string.
    """
    normalised = division.lower().replace(" ", "-")
    if normalised in _DIVISION_ROLE_MAP:
        return _DIVISION_ROLE_MAP[normalised]
    base = re.split(r"[_-]", normalised)[0]
    return _DIVISION_ROLE_MAP.get(base, base)


def _is_non_software_agent(name: str) -> bool:
    """Return True if the agent name matches a non-software-engineering signal."""
    name_lower = name.lower()
    return any(sig in name_lower for sig in _NON_SOFTWARE_NAME_SIGNALS)


def _infer_role(
    name: str,
    description: str,
    capabilities: list[str],
    division_role: str,
) -> str:
    """Infer the best Bernstein role from agent metadata.

    Scores the agent's **name** against keyword signals for each Bernstein
    role.  Only the name is used (not description) because descriptions
    contain too many incidental keywords that cause false positives
    (e.g. "security guardrails" on a cost-optimization agent).

    If a clear winner emerges (score >= 6), that role overrides the
    division-based default.

    Args:
        name: Agent display name (e.g. "Code Reviewer").
        description: Agent description text (reserved for future use).
        capabilities: List of capability keywords (reserved for future use).
        division_role: Fallback role derived from the Agency division.

    Returns:
        The inferred Bernstein role, or *division_role* if no signal is strong
        enough.
    """
    name_lower = name.lower()

    best_role = division_role
    best_score = 0

    for role, signals in _ROLE_SIGNALS_NAME.items():
        score = 0
        for phrase, weight in signals:
            if phrase in name_lower:
                score += weight
        if score > best_score:
            best_score = score
            best_role = role

    # Only override division role if signal is strong enough.
    if best_score >= 6:
        if best_role != division_role:
            logger.debug(
                "Role override for '%s': %s -> %s (score=%d)",
                name,
                division_role,
                best_role,
                best_score,
            )
        return best_role

    return division_role


class AgencyProvider:
    """Provider that reads Agency-format markdown files from a local directory.

    Args:
        local_path: Root of the local Agency repo clone.
    """

    def __init__(self, local_path: Path) -> None:
        self._local_path = local_path

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    def provider_id(self) -> str:
        """Return the unique provider identifier ``"agency"``."""
        return "agency"

    def is_available(self) -> bool:
        """Return ``True`` if the local_path directory exists."""
        return self._local_path.is_dir()

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
        """Extract YAML frontmatter and body from *text*.

        Args:
            text: Raw file contents.

        Returns:
            ``(frontmatter_dict, body_text)`` or ``None`` if no frontmatter.
        """
        if not text.startswith("---"):
            return None
        rest = text[3:]  # skip opening "---"
        end = rest.find("\n---")
        if end == -1:
            return None
        fm_text = rest[:end]
        body_raw = rest[end + 4 :]  # skip "\n---"
        body = body_raw.lstrip("\n")
        try:
            fm: Any = yaml.safe_load(fm_text)
        except yaml.YAMLError:
            return None
        if not isinstance(fm, dict):
            return None
        return (fm, body)  # type: ignore[reportUnknownVariableType]

    @staticmethod
    def _parse_file(path: Path, division: str) -> list[CatalogAgent]:
        """Parse a single Agency markdown file into ``CatalogAgent`` instances.

        Extracts ``name``, ``description``, ``capabilities``, and ``tools``
        from the YAML frontmatter, and uses the markdown body as the system
        prompt.

        Args:
            path: Path to a ``.md`` file.
            division: Agency division name (parent subdirectory name).

        Returns:
            A list containing one ``CatalogAgent``, or an empty list if the
            file is skipped (missing/empty name, no frontmatter, read error).
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []

        result = AgencyProvider._parse_frontmatter(text)
        if result is None:
            return []

        fm, body = result
        name: str = str(fm.get("name") or "").strip()
        if not name:
            return []

        # Skip agents that are clearly not software engineering personas
        if _is_non_software_agent(name):
            logger.debug("Skipping non-software agent: '%s'", name)
            return []

        description: str = str(fm.get("description") or "")
        division_role = _division_to_role(division)
        agent_id = f"agency:{_slugify(name)}"

        # Extract capabilities list (e.g. [api-design, authentication, jwt])
        raw_caps: list[Any] = list(fm.get("capabilities") or [])
        capabilities: list[str] = [str(c) for c in raw_caps]

        # Extract preferred tools list (e.g. [pytest, ruff, mypy])
        raw_tools: list[Any] = list(fm.get("tools") or [])
        tools: list[str] = [str(t) for t in raw_tools]

        # Infer role from agent metadata instead of relying solely on division
        role = _infer_role(name, description, capabilities, division_role)

        return [
            CatalogAgent(
                name=name,
                role=role,
                description=description,
                system_prompt=body,
                id=agent_id,
                tools=tools,
                capabilities=capabilities,
                priority=100,
                source="agency",
            )
        ]

    # ------------------------------------------------------------------
    # Async interface
    # ------------------------------------------------------------------

    async def fetch_agents(self) -> list[CatalogAgent]:
        """Scan subdirectories of *local_path* for Agency markdown files.

        Returns:
            All successfully parsed ``CatalogAgent`` instances.
        """
        await asyncio.sleep(0)  # Async interface requirement
        if not self.is_available():
            return []

        agents: list[CatalogAgent] = []
        for division_dir in sorted(self._local_path.iterdir()):
            if not division_dir.is_dir():
                continue
            division = division_dir.name
            if division.lower() in _NON_SOFTWARE_DIVISIONS:
                logger.debug("Skipping non-software division: %s", division)
                continue
            for md_file in sorted(division_dir.glob("*.md")):
                agents.extend(self._parse_file(md_file, division))

        return agents

    async def refresh(self) -> list[CatalogAgent]:
        """Re-scan *local_path* and return all agents.

        In local-path mode this is equivalent to :meth:`fetch_agents`.

        Returns:
            All parsed ``CatalogAgent`` instances.
        """
        return await self.fetch_agents()

    # ------------------------------------------------------------------
    # Auto-sync helpers
    # ------------------------------------------------------------------

    @classmethod
    def default_cache_path(cls) -> Path:
        """Return the default Agency catalog cache path: ``~/.bernstein/catalogs/agency``."""
        return Path.home() / ".bernstein" / "catalogs" / "agency"

    @classmethod
    def sync_catalog(
        cls,
        target: Path | None = None,
        url: str = _DEFAULT_AGENCY_SOURCE,
        *,
        force: bool = False,
    ) -> tuple[bool, str]:
        """Clone or update the Agency catalog repo to *target*.

        On first call, does a shallow ``git clone``.  On subsequent calls,
        does ``git pull --ff-only``.  Skips the network request if the last
        sync was less than ``_SYNC_TTL_SECONDS`` ago, unless *force* is True.

        Args:
            target: Local directory to clone into.  Defaults to
                :meth:`default_cache_path`.
            url: Remote git URL to clone from.
            force: Bypass the TTL check and always sync.

        Returns:
            ``(success, message)`` where *message* is suitable for display.
        """
        if target is None:
            target = cls.default_cache_path()

        # TTL check — skip if synced recently
        marker = target.parent / f".{target.name}.synced"
        if not force and marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < _SYNC_TTL_SECONDS:
                return True, f"up to date (synced {age / 3600:.1f}h ago)"

        if target.exists() and (target / ".git").exists():
            # Existing clone — just pull
            result = subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only", "--quiet"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.returncode != 0:
                return False, f"git pull failed: {result.stderr.strip()}"
            action = "updated"
        else:
            # Fresh clone (shallow to keep it fast)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                import shutil

                shutil.rmtree(target)
            result = subprocess.run(
                ["git", "clone", "--depth=1", "--quiet", url, str(target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if result.returncode != 0:
                return False, f"git clone failed: {result.stderr.strip()}"
            action = "cloned"

        marker.touch()
        return True, f"Agency catalog {action} from {url}"
