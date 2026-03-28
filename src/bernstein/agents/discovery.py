"""Agent directory auto-discovery for Bernstein.

Scans known sources for agent catalogs and maintains a registry at
``.sdd/agents/registry.json``.

Supported sources:
- **local**: ``~/.bernstein/agents/`` — user-level agent definitions.
- **project**: ``.sdd/agents/local/`` — project-level custom agents.
- **github**: Repos tagged ``bernstein-agents`` or containing
  ``.bernstein-catalog.yaml`` (requires network access).
- **npm**: Packages with ``bernstein-agent`` keyword (requires network access).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

DiscoverySourceType = Literal["local", "project", "github", "npm"]

_REGISTRY_PATH = Path(".sdd/agents/registry.json")
_USER_AGENTS_DIR = Path.home() / ".bernstein" / "agents"
_PROJECT_AGENTS_DIR = Path(".sdd/agents/local")


@dataclass
class DirectoryEntry:
    """A discovered agent directory source.

    Attributes:
        name: Human-readable source name.
        source_type: One of ``local``, ``project``, ``github``, ``npm``.
        url: Remote URL for GitHub/npm sources.
        path: Local filesystem path for local/project sources.
        agents: Number of agents found in this directory.
        last_sync: ISO-8601 timestamp of the last sync.
        enabled: Whether this directory is active.
    """

    name: str
    source_type: DiscoverySourceType
    url: str | None = None
    path: str | None = None
    agents: int = 0
    last_sync: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialise to JSON-compatible dict."""
        return {
            "name": self.name,
            "source_type": self.source_type,
            "url": self.url,
            "path": self.path,
            "agents": self.agents,
            "last_sync": self.last_sync,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DirectoryEntry:
        """Deserialise from a JSON-compatible dict."""
        return cls(
            name=d["name"],
            source_type=d.get("source_type", "local"),
            url=d.get("url"),
            path=d.get("path"),
            agents=d.get("agents", 0),
            last_sync=d.get("last_sync"),
            enabled=d.get("enabled", True),
        )


@dataclass
class AgentMetrics:
    """Per-source success rate metrics.

    Attributes:
        source: Source name (e.g. "agency", "local", "builtin").
        tasks_assigned: Total tasks routed to this source.
        tasks_succeeded: Tasks that completed successfully.
        tasks_failed: Tasks that failed verification.
    """

    source: str
    tasks_assigned: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0

    @property
    def success_rate(self) -> float:
        """Float in [0, 1]; returns 0.0 when no tasks assigned."""
        if self.tasks_assigned == 0:
            return 0.0
        return self.tasks_succeeded / self.tasks_assigned

    def to_dict(self) -> dict[str, Any]:
        """Serialise to JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentMetrics:
        """Deserialise from a JSON-compatible dict."""
        return cls(
            source=d["source"],
            tasks_assigned=d.get("tasks_assigned", 0),
            tasks_succeeded=d.get("tasks_succeeded", 0),
            tasks_failed=d.get("tasks_failed", 0),
        )


@dataclass
class AgentDiscovery:
    """Discovers and tracks agent directories from multiple sources.

    Maintains a registry at ``.sdd/agents/registry.json`` that records
    known directories, total agent counts, and per-source success metrics.
    These metrics are consumed by the ``agents showcase`` command and feed
    into the evolution loop to bias task routing toward higher-performing
    agent sources.

    Attributes:
        registry_path: Path to the JSON registry file.
        directories: Known agent directory entries.
        metrics: Per-source success rate metrics.
        total_agents: Aggregate agent count across all directories.
        last_full_sync: ISO-8601 timestamp of the most recent full sync.
    """

    registry_path: Path = field(default_factory=lambda: _REGISTRY_PATH)
    directories: list[DirectoryEntry] = field(default_factory=list[DirectoryEntry])
    metrics: dict[str, AgentMetrics] = field(default_factory=dict[str, AgentMetrics])
    total_agents: int = 0
    last_full_sync: str | None = None

    # ------------------------------------------------------------------ #
    # Factory / persistence                                                #
    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, registry_path: Path = _REGISTRY_PATH) -> AgentDiscovery:
        """Load an existing registry from disk, or return a fresh instance.

        Args:
            registry_path: Path to ``registry.json``.

        Returns:
            Populated ``AgentDiscovery`` instance.
        """
        instance = cls(registry_path=registry_path)
        if registry_path.exists():
            try:
                raw = json.loads(registry_path.read_text(encoding="utf-8"))
                instance._from_raw(raw)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Registry at %s is unreadable (%s); starting fresh", registry_path, exc)
        return instance

    def save(self) -> None:
        """Persist the registry to ``registry_path``."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(self._to_raw(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug("Registry saved to %s", self.registry_path)

    # ------------------------------------------------------------------ #
    # Discovery                                                            #
    # ------------------------------------------------------------------ #

    def discover_local(self) -> int:
        """Scan ``~/.bernstein/agents/`` for user-level agent definitions.

        Counts YAML/YML files found and updates (or adds) the corresponding
        ``DirectoryEntry``.

        Returns:
            Number of agent files found.
        """
        return self._scan_local_dir(
            name="local",
            source_type="local",
            path=_USER_AGENTS_DIR,
        )

    def discover_project(self) -> int:
        """Scan ``.sdd/agents/local/`` for project-level custom agents.

        Returns:
            Number of agent files found.
        """
        return self._scan_local_dir(
            name="project",
            source_type="project",
            path=_PROJECT_AGENTS_DIR,
        )

    def discover_github(self, *, timeout: float = 5.0) -> list[DirectoryEntry]:
        """Search GitHub for repos tagged ``bernstein-agents``.

        Performs a live search via the GitHub API (unauthenticated, rate-limited
        to 10 req/min).  Results are deduplicated against existing entries.

        Args:
            timeout: HTTP request timeout in seconds.

        Returns:
            List of newly discovered ``DirectoryEntry`` items (may be empty if
            the network is unreachable or no new repos are found).
        """
        try:
            import urllib.request

            url = "https://api.github.com/search/repositories?q=topic:bernstein-agents&sort=stars&per_page=10"
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "bernstein-agent-discovery/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            logger.debug("GitHub discovery skipped: %s", exc)
            return []

        added: list[DirectoryEntry] = []
        existing_urls = {d.url for d in self.directories if d.url}
        for item in data.get("items", []):
            html_url = item.get("html_url", "")
            if not html_url or html_url in existing_urls:
                continue
            entry = DirectoryEntry(
                name=item.get("name", html_url),
                source_type="github",
                url=html_url,
                agents=0,
                last_sync=_now_iso(),
            )
            self.directories.append(entry)
            added.append(entry)
            existing_urls.add(html_url)
            logger.info("GitHub discovery: found %s", html_url)

        return added

    def discover_npm(self, *, timeout: float = 5.0) -> list[DirectoryEntry]:
        """Search npm for packages with the ``bernstein-agent`` keyword.

        Args:
            timeout: HTTP request timeout in seconds.

        Returns:
            List of newly discovered ``DirectoryEntry`` items.
        """
        try:
            import urllib.request

            url = "https://registry.npmjs.org/-/v1/search?text=keywords:bernstein-agent&size=10"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "bernstein-agent-discovery/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            logger.debug("npm discovery skipped: %s", exc)
            return []

        added: list[DirectoryEntry] = []
        existing_names = {d.name for d in self.directories}
        for obj in data.get("objects", []):
            pkg = obj.get("package", {})
            pkg_name = pkg.get("name", "")
            if not pkg_name or pkg_name in existing_names:
                continue
            npm_url = f"https://www.npmjs.com/package/{pkg_name}"
            entry = DirectoryEntry(
                name=pkg_name,
                source_type="npm",
                url=npm_url,
                agents=0,
                last_sync=_now_iso(),
            )
            self.directories.append(entry)
            added.append(entry)
            existing_names.add(pkg_name)
            logger.info("npm discovery: found %s", pkg_name)

        return added

    def full_sync(self, *, include_network: bool = False) -> dict[str, int]:
        """Run all local discovery steps (and optionally network steps).

        Always scans the local user directory and the project local directory.
        When *include_network* is True also queries GitHub and npm.

        Args:
            include_network: If ``True``, also perform GitHub and npm searches.

        Returns:
            Dict mapping source name → number of agents found.
        """
        results: dict[str, int] = {}
        results["local"] = self.discover_local()
        results["project"] = self.discover_project()

        if include_network:
            gh_entries = self.discover_github()
            results["github"] = len(gh_entries)
            npm_entries = self.discover_npm()
            results["npm"] = len(npm_entries)

        self.total_agents = sum(d.agents for d in self.directories)
        self.last_full_sync = _now_iso()
        self.save()
        return results

    # ------------------------------------------------------------------ #
    # Metrics                                                              #
    # ------------------------------------------------------------------ #

    def record_task_outcome(self, source: str, *, succeeded: bool) -> None:
        """Update success metrics for an agent source.

        Args:
            source: Agent source label (e.g. ``"agency"``, ``"builtin"``).
            succeeded: Whether the task passed verification.
        """
        if source not in self.metrics:
            self.metrics[source] = AgentMetrics(source=source)
        m = self.metrics[source]
        m.tasks_assigned += 1
        if succeeded:
            m.tasks_succeeded += 1
        else:
            m.tasks_failed += 1

    def success_rate(self, source: str) -> float:
        """Return success rate for a given source (0.0 if unknown).

        Args:
            source: Agent source label.

        Returns:
            Float in [0, 1].
        """
        m = self.metrics.get(source)
        return m.success_rate if m is not None else 0.0

    def top_sources(self, *, min_tasks: int = 3) -> list[AgentMetrics]:
        """Return sources sorted by descending success rate.

        Only includes sources with at least *min_tasks* assignments.

        Args:
            min_tasks: Minimum tasks threshold.

        Returns:
            List of ``AgentMetrics`` sorted descending by success rate.
        """
        candidates = [m for m in self.metrics.values() if m.tasks_assigned >= min_tasks]
        return sorted(candidates, key=lambda m: m.success_rate, reverse=True)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _scan_local_dir(
        self,
        *,
        name: str,
        source_type: DiscoverySourceType,
        path: Path,
    ) -> int:
        """Count YAML/MD agent files in *path* and update the registry entry.

        Args:
            name: Directory entry name.
            source_type: Source type tag.
            path: Directory to scan.

        Returns:
            Number of files found (0 if directory does not exist).
        """
        count = 0
        if path.exists() and path.is_dir():
            count = sum(1 for p in path.iterdir() if p.suffix in (".yaml", ".yml", ".md") and p.is_file())

        ts = _now_iso()
        # Update existing entry or append new one
        for entry in self.directories:
            if entry.name == name:
                entry.agents = count
                entry.last_sync = ts
                return count

        self.directories.append(
            DirectoryEntry(
                name=name,
                source_type=source_type,
                path=str(path),
                agents=count,
                last_sync=ts,
            )
        )
        return count

    def _to_raw(self) -> dict[str, Any]:
        return {
            "directories": [d.to_dict() for d in self.directories],
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "total_agents": self.total_agents,
            "last_full_sync": self.last_full_sync,
        }

    def _from_raw(self, raw: dict[str, Any]) -> None:
        self.directories = [DirectoryEntry.from_dict(d) for d in raw.get("directories", [])]
        self.metrics = {k: AgentMetrics.from_dict(v) for k, v in raw.get("metrics", {}).items()}
        self.total_agents = raw.get("total_agents", 0)
        self.last_full_sync = raw.get("last_full_sync")


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
