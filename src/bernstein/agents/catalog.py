"""Agent catalog registry — loads agent definitions from external sources.

Supports two catalog types:
- ``agency``: Remote Agency-format agent catalog (GitHub repo or local path).
- ``generic``: Local directory of YAML files with a configurable field map.

Also provides role-based agent matching via ``CatalogRegistry.match()``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from bernstein.core.agency_loader import AgencyAgent

logger = logging.getLogger(__name__)

CatalogType = Literal["agency", "generic"]

_DEFAULT_AGENCY_SOURCE = "https://github.com/msitarzewski/agency-agents"
_CACHE_FILE = Path(".sdd/agents/catalog.json")
_REMOTE_TTL = 3600  # 1 hour — default TTL for remote provider entries
_LOCAL_TTL = 300  # 5 minutes — default TTL for local provider entries

# Hardcoded fallback roles used when providers and cache both fail.
_BUILTIN_AGENT_ENTRIES: list[dict[str, Any]] = [
    {"role": "manager", "description": "Plans and decomposes goals into tasks.", "model": "opus", "effort": "max"},
    {"role": "backend", "description": "Backend engineer.", "model": "sonnet", "effort": "high"},
    {"role": "frontend", "description": "Frontend engineer.", "model": "sonnet", "effort": "high"},
    {"role": "qa", "description": "Quality assurance and test engineer.", "model": "sonnet", "effort": "normal"},
    {"role": "security", "description": "Security engineer.", "model": "sonnet", "effort": "high"},
    {"role": "devops", "description": "DevOps / infrastructure engineer.", "model": "sonnet", "effort": "normal"},
    {"role": "architect", "description": "System architect.", "model": "opus", "effort": "high"},
    {"role": "reviewer", "description": "Code reviewer.", "model": "sonnet", "effort": "normal"},
    {"role": "docs", "description": "Documentation writer.", "model": "sonnet", "effort": "normal"},
    {"role": "ml-engineer", "description": "Machine-learning engineer.", "model": "sonnet", "effort": "high"},
]


@dataclass(frozen=True)
class CatalogAgent:
    """An agent loaded from a catalog, ready for prompt sourcing.

    Attributes:
        name: Human-readable agent name.
        role: Bernstein role name (e.g. "backend", "security").
        description: Short description of agent capabilities.
        system_prompt: Full system prompt text for this agent.
        id: Unique catalog identifier, e.g. ``agency:code-reviewer``.
        tools: Tool names the agent prefers (e.g. "pytest", "ruff").
        capabilities: Declared capability keywords for task matching
            (e.g. "api-design", "authentication", "jwt").
        priority: Matching priority — lower value wins (default 100).
        source: Origin label (e.g. "catalog", "agency").
    """

    name: str
    role: str
    description: str
    system_prompt: str
    id: str = ""
    tools: list[str] = field(default_factory=list[str])
    capabilities: list[str] = field(default_factory=list[str])
    priority: int = 100
    source: str = "catalog"


@dataclass(frozen=True)
class CatalogEntry:
    """Configuration for a single agent catalog source.

    Attributes:
        name: Unique identifier for this catalog.
        type: Provider type — ``"agency"`` or ``"generic"``.
        enabled: Whether this catalog is active.
        priority: Load priority; higher values are checked first.
        source: Remote source URL (agency type only).
        path: Local directory path (generic type, or agency local override).
        format: File format for generic catalogs (e.g. ``"yaml"``).
        glob: Glob pattern for generic catalog file discovery.
        field_map: Mapping from generic YAML field names to canonical names
            (``id``, ``name``, ``role``, ``system_prompt``).
    """

    name: str
    type: CatalogType
    enabled: bool = True
    priority: int = 50
    source: str | None = None
    path: str | None = None
    format: str | None = None
    glob: str | None = None
    field_map: dict[str, str] = field(default_factory=dict[str, str])


@dataclass
class CachedAgentEntry:
    """A cached agent role entry with TTL metadata.

    Written to ``.sdd/agents/catalog.json`` after each provider sync.

    Attributes:
        role: Unique role identifier (e.g. "backend", "qa").
        description: Human-readable description of agent capabilities.
        model: Default model (e.g. "sonnet", "opus").
        effort: Default effort level ("max", "high", "normal", "low").
        source: Provider name that supplied this entry (or "builtin").
        fetched_at: Unix timestamp when this entry was fetched.
        ttl_seconds: How long this entry is considered fresh.
        metadata: Additional arbitrary data from the provider.
    """

    role: str
    description: str
    model: str
    effort: str
    source: str
    fetched_at: float
    ttl_seconds: int
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def is_fresh(self) -> bool:
        """True if the entry is within its TTL window."""
        return time.time() - self.fetched_at < self.ttl_seconds


@dataclass
class CatalogRegistry:
    """Registry of agent catalog providers ordered by priority.

    Entries are sorted descending by ``priority`` so that high-priority
    catalogs are queried first.  The registry also stores loaded
    ``CatalogAgent`` instances and exposes a ``match()`` method so the
    spawner can look up the best agent for a role before falling back to
    built-in templates.

    Attributes:
        entries: Ordered list of enabled catalog entries.
        loaded_agents: Agents loaded from catalogs (populated via
            ``load_from_agency()`` or ``register_agent()``).
    """

    entries: list[CatalogEntry] = field(default_factory=list[CatalogEntry])
    loaded_agents: list[CatalogAgent] = field(default_factory=list[CatalogAgent], repr=False)
    _cache_path: Path = field(default_factory=lambda: _CACHE_FILE, repr=False, compare=False)
    _cached_roles: dict[str, CachedAgentEntry] = field(
        default_factory=dict[str, CachedAgentEntry],
        repr=False,
        compare=False,
    )

    @classmethod
    def from_config(cls, catalogs_config: list[dict[str, Any]]) -> CatalogRegistry:
        """Build a registry from the ``catalogs`` section of bernstein.yaml.

        Args:
            catalogs_config: Parsed YAML list of catalog mapping objects.

        Returns:
            CatalogRegistry with entries sorted by descending priority.

        Raises:
            ValueError: If a catalog entry is missing required fields or has
                an unrecognised type.
        """
        entries: list[CatalogEntry] = []
        for raw in catalogs_config:
            entry = _parse_catalog_entry(raw)
            if entry.enabled:
                entries.append(entry)

        entries.sort(key=lambda e: e.priority, reverse=True)
        return cls(entries=entries)

    @classmethod
    def default(cls) -> CatalogRegistry:
        """Return the default registry: Agency provider in remote mode.

        Returns:
            CatalogRegistry with a single enabled Agency entry.
        """
        default_entry = CatalogEntry(
            name="agency",
            type="agency",
            enabled=True,
            priority=100,
            source=_DEFAULT_AGENCY_SOURCE,
        )
        return cls(entries=[default_entry])

    # -- Agent matching -------------------------------------------------------

    def register_agent(self, agent: CatalogAgent) -> None:
        """Add a single CatalogAgent to the loaded pool.

        Args:
            agent: Agent to register.
        """
        self.loaded_agents.append(agent)
        logger.debug("Registered catalog agent '%s' for role '%s'", agent.name, agent.role)

    def load_from_agency(self, agency_catalog: dict[str, AgencyAgent]) -> int:
        """Bulk-load agents from an Agency catalog dict.

        Converts ``AgencyAgent.prompt_body`` to ``CatalogAgent.system_prompt``.
        Agents without a prompt body are skipped.

        Args:
            agency_catalog: Mapping of agent name → AgencyAgent as returned by
                ``agency_loader.load_agency_catalog()``.

        Returns:
            Number of agents successfully loaded.
        """
        loaded = 0
        for agent in agency_catalog.values():
            if not agent.prompt_body:
                continue
            self.loaded_agents.append(
                CatalogAgent(
                    name=agent.name,
                    role=agent.role,
                    description=agent.description,
                    system_prompt=agent.prompt_body,
                    priority=100,
                    source="agency",
                )
            )
            loaded += 1
        logger.info("Loaded %d agents from agency catalog", loaded)
        return loaded

    # -- Cache management -----------------------------------------------------

    def write_cache(self) -> None:
        """Serialise ``_cached_roles`` to the JSON cache file.

        Creates parent directories as needed.  Existing file is overwritten.
        """
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [asdict(entry) for entry in self._cached_roles.values()]
        self._cache_path.write_text(json.dumps(rows, indent=2))
        logger.debug("Wrote %d cached role(s) to %s", len(rows), self._cache_path)

    def load_cache(self) -> bool:
        """Load fresh entries from the JSON cache file into ``_cached_roles``.

        Entries whose TTL has expired are silently skipped.  If the file is
        missing, corrupt, or contains no fresh entries the method returns
        ``False`` so the caller knows a provider refresh is needed.

        Returns:
            ``True`` if at least one fresh entry was loaded, ``False`` otherwise.
        """
        if not self._cache_path.exists():
            return False
        try:
            raw_list = json.loads(self._cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Catalog cache at %s is unreadable", self._cache_path)
            return False

        loaded = 0
        try:
            for row in raw_list:
                entry = CachedAgentEntry(
                    role=row["role"],
                    description=row["description"],
                    model=row["model"],
                    effort=row["effort"],
                    source=row["source"],
                    fetched_at=float(row["fetched_at"]),
                    ttl_seconds=int(row["ttl_seconds"]),
                    metadata=row.get("metadata", {}),
                )
                if entry.is_fresh:
                    self._cached_roles[entry.role] = entry
                    loaded += 1
        except (KeyError, TypeError, ValueError):
            logger.warning("Catalog cache %s contains invalid entries", self._cache_path)
            self._cached_roles.clear()
            return False

        logger.debug("Loaded %d fresh role(s) from cache %s", loaded, self._cache_path)
        return loaded > 0

    def discover(self, *, force: bool = False) -> None:
        """Discover agents from providers, with TTL-based local cache.

        On a normal (non-forced) call:
        1. Try to load the local cache.  If fresh entries exist, return early.
        2. Fetch from all enabled providers in priority order.
        3. Fall back to built-in roles if all providers fail or none are
           configured.  Built-in roles never overwrite higher-priority entries
           already present in ``_cached_roles``.
        4. Write the merged result to the cache file.

        When *force* is ``True``:
        - Skip the cache check and clear any existing ``_cached_roles``.
        - Re-fetch from providers / builtins unconditionally.
        - Write the refreshed cache.

        Args:
            force: If ``True``, bypass the TTL check and re-fetch everything.
        """
        if not force and self.load_cache():
            logger.info("Catalog: using %d fresh cached role(s)", len(self._cached_roles))
            return

        if force:
            self._cached_roles.clear()

        # Attempt to fetch from configured providers.
        # Providers are sorted descending by priority (done in from_config).
        fetched_any = self._fetch_from_providers()

        # Graceful degradation: load built-in roles for any role not yet
        # covered (or for all roles when providers completely failed).
        now = time.time()
        ttl = _LOCAL_TTL
        for raw in _BUILTIN_AGENT_ENTRIES:
            role = raw["role"]
            if role not in self._cached_roles:
                self._cached_roles[role] = CachedAgentEntry(
                    role=role,
                    description=raw.get("description", ""),
                    model=raw.get("model", "sonnet"),
                    effort=raw.get("effort", "normal"),
                    source="builtin",
                    fetched_at=now,
                    ttl_seconds=ttl,
                )

        if not fetched_any:
            logger.info("Catalog: no providers available — using built-in roles")

        self.write_cache()
        logger.info(
            "Catalog: discovered %d role(s) (%s)",
            len(self._cached_roles),
            "forced" if force else "refreshed",
        )

    def _fetch_from_providers(self) -> bool:
        """Attempt to load agents from each configured CatalogEntry.

        Iterates providers in their sorted priority order (highest first).
        Higher-priority providers win on role conflicts — an entry already
        present in ``_cached_roles`` is never overwritten.

        Returns:
            ``True`` if at least one provider loaded at least one entry.
        """
        if not self.entries:
            return False

        now = time.time()
        fetched_any = False

        for entry in self.entries:
            if not entry.enabled:
                continue
            ttl = _LOCAL_TTL if entry.path else _REMOTE_TTL
            try:
                roles = self._load_entry(entry)
            except Exception:
                logger.warning("Provider '%s' failed to load", entry.name, exc_info=True)
                continue

            for role, meta in roles.items():
                if role not in self._cached_roles:
                    self._cached_roles[role] = CachedAgentEntry(
                        role=role,
                        description=meta.get("description", ""),
                        model=meta.get("model", "sonnet"),
                        effort=meta.get("effort", "normal"),
                        source=entry.name,
                        fetched_at=now,
                        ttl_seconds=ttl,
                        metadata={k: v for k, v in meta.items() if k not in ("role", "description", "model", "effort")},
                    )
                    fetched_any = True

        return fetched_any

    def _load_entry(self, entry: CatalogEntry) -> dict[str, dict[str, Any]]:
        """Load role metadata from a single CatalogEntry.

        Args:
            entry: The catalog source to load from.

        Returns:
            Mapping of role name → metadata dict.
        """
        if entry.type == "agency" and entry.path:
            from pathlib import Path as _Path

            from bernstein.core.agency_loader import load_agency_catalog

            catalog_dir = _Path(entry.path)
            agents = load_agency_catalog(catalog_dir)
            return {
                a.role: {"description": a.description, "model": "sonnet", "effort": "normal"} for a in agents.values()
            }

        if entry.type == "generic" and entry.path:
            return self._load_generic_entry(entry)

        # Remote agency (no local path) — not fetched at discover time
        logger.debug("Skipping remote provider '%s' (no local path configured)", entry.name)
        return {}

    def _load_generic_entry(self, entry: CatalogEntry) -> dict[str, dict[str, Any]]:
        """Load role metadata from a generic local YAML catalog.

        Args:
            entry: A catalog entry with ``type="generic"``.

        Returns:
            Mapping of role name → metadata dict.
        """
        import glob as _glob
        from pathlib import Path as _Path

        import yaml

        catalog_dir = _Path(entry.path)  # type: ignore[arg-type]
        pattern = entry.glob or "*.yaml"
        fm = entry.field_map

        results: dict[str, dict[str, Any]] = {}
        for file_path in _glob.glob(str(catalog_dir / pattern)):
            try:
                raw_data: object = yaml.safe_load(_Path(file_path).read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Skipping unreadable generic catalog file: %s", file_path)
                continue
            if not isinstance(raw_data, dict):
                continue
            raw: dict[str, Any] = cast("dict[str, Any]", raw_data)
            role: str | None = raw.get(fm.get("role", "role"), raw.get("role"))
            if not role:
                continue
            results[str(role)] = {
                "description": str(raw.get(fm.get("description", "description"), "")),
                "model": str(raw.get(fm.get("model", "model"), "sonnet")),
                "effort": str(raw.get(fm.get("effort", "effort"), "normal")),
            }
        return results

    # -- Agent matching -------------------------------------------------------

    def match(self, role: str, task_description: str) -> CatalogAgent | None:
        """Find the best catalog agent for a role and task description.

        Matching strategy:
        1. Collect all agents whose ``role`` exactly matches *role*.
        2. Among those, rank by capability overlap with *task_description*
           (capabilities weighted 2x), then by ``priority``.
        3. If no exact role match, fall back to capability + description
           keyword matching across all agents.

        Args:
            role: Bernstein role name to match (e.g. ``"security"``).
            task_description: Task description used for capability and keyword
                matching.

        Returns:
            Best-matching ``CatalogAgent``, or ``None`` if no candidates.
        """
        if not self.loaded_agents:
            return None

        desc_lower = task_description.lower()
        keywords = {w for w in desc_lower.split() if len(w) > 3}

        # 1. Exact role match — rank by capability overlap then priority.
        #    Require a minimum match quality to avoid assigning irrelevant
        #    Agency agents (e.g. "Email Intelligence Engineer" for a coding task).
        _MIN_MATCH_SCORE = 3  # At least 3 keyword overlaps required.
        exact: list[CatalogAgent] = [a for a in self.loaded_agents if a.role == role]
        if exact:
            if keywords:
                scored_exact = [(_capability_score(a, desc_lower, keywords), a) for a in exact]
                if all(score == 0 for score, _ in scored_exact):
                    scored_exact = [
                        (len(keywords & {w for w in a.description.lower().split() if len(w) > 3}), a) for a in exact
                    ]
                scored_exact.sort(key=lambda t: (-t[0], t[1].priority))
                best_score = scored_exact[0][0] if scored_exact else 0
                if best_score >= _MIN_MATCH_SCORE:
                    winner = scored_exact[0][1]
                    logger.debug(
                        "Catalog exact match: '%s' (score=%d) for '%s'",
                        winner.name,
                        best_score,
                        role,
                    )
                    return winner
                # Score too low — skip catalog, use role template instead.
                logger.debug(
                    "Catalog: best score %d < %d for '%s', using template",
                    best_score,
                    _MIN_MATCH_SCORE,
                    role,
                )
                return None
            else:
                return None  # No keywords — can't score, use role template.

        if not keywords:
            return None

        # 2. Fuzzy match: capabilities (x2) + description keyword overlap.
        #    Require minimum score to avoid irrelevant cross-role matches.
        scored: list[tuple[int, CatalogAgent]] = []
        for agent in self.loaded_agents:
            cap_score = _capability_score(agent, desc_lower, keywords) * 2
            desc_score = len(keywords & {w for w in agent.description.lower().split() if len(w) > 3})
            total = cap_score + desc_score
            if total >= _MIN_MATCH_SCORE:
                scored.append((total, agent))

        if not scored:
            return None

        scored.sort(key=lambda t: (-t[0], t[1].priority))
        winner = scored[0][1]
        logger.debug(
            "Catalog fuzzy match: agent '%s' (role=%s) for role '%s'",
            winner.name,
            winner.role,
            role,
        )
        return winner


def _capability_score(agent: CatalogAgent, desc_lower: str, keywords: set[str]) -> int:
    """Score *agent* by how many of its capabilities match the task keywords.

    Each capability is normalised (hyphens/underscores → spaces) then checked
    against *desc_lower* and *keywords* for any overlap.

    Args:
        agent: The catalog agent to score.
        desc_lower: Lowercase task description string.
        keywords: Set of keyword tokens extracted from the description.

    Returns:
        Integer overlap count (0 when agent has no capabilities).
    """
    score = 0
    for cap in agent.capabilities:
        cap_norm = cap.lower().replace("-", " ").replace("_", " ")
        if cap_norm in desc_lower or any(kw in cap_norm for kw in keywords):
            score += 1
    return score


def _parse_catalog_entry(raw: dict[str, Any]) -> CatalogEntry:
    """Parse and validate a single catalog entry from YAML data.

    Args:
        raw: Dictionary from YAML representing one catalog entry.

    Returns:
        Validated CatalogEntry.

    Raises:
        ValueError: If required fields are missing or values are invalid.
    """
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"catalog entry missing required string 'name': {raw!r}")

    catalog_type = raw.get("type")
    if catalog_type not in ("agency", "generic"):
        raise ValueError(f"catalog '{name}': type must be 'agency' or 'generic', got {catalog_type!r}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"catalog '{name}': enabled must be a bool, got {type(enabled).__name__}")

    priority = raw.get("priority", 50)
    if not isinstance(priority, int):
        raise ValueError(f"catalog '{name}': priority must be an int, got {type(priority).__name__}")

    source = raw.get("source")
    if source is not None and not isinstance(source, str):
        raise ValueError(f"catalog '{name}': source must be a string")

    path = raw.get("path")
    if path is not None and not isinstance(path, str):
        raise ValueError(f"catalog '{name}': path must be a string")

    fmt = raw.get("format")
    if fmt is not None and not isinstance(fmt, str):
        raise ValueError(f"catalog '{name}': format must be a string")

    glob_pattern = raw.get("glob")
    if glob_pattern is not None and not isinstance(glob_pattern, str):
        raise ValueError(f"catalog '{name}': glob must be a string")

    field_map_raw: object = raw.get("field_map", {})
    if not isinstance(field_map_raw, dict):
        raise ValueError(f"catalog '{name}': field_map must be a string-to-string mapping")
    field_map_checked: dict[str, Any] = cast("dict[str, Any]", field_map_raw)
    if not all(isinstance(v, str) for v in field_map_checked.values()):
        raise ValueError(f"catalog '{name}': field_map must be a string-to-string mapping")

    field_map_typed: dict[str, str] = {str(k): str(v) for k, v in field_map_checked.items()}

    return CatalogEntry(
        name=name,
        type=catalog_type,  # type: ignore[arg-type]
        enabled=enabled,
        priority=priority,
        source=source,
        path=path,
        format=fmt,
        glob=glob_pattern,
        field_map=field_map_typed,
    )
