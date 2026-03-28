"""AgencyProvider — loads CatalogAgent instances from msitarzewski/agency-agents format.

Agency repos use one Markdown file per agent, organised into division
subdirectories. Each file has YAML frontmatter (name, description, …)
followed by the system-prompt body.
"""

from __future__ import annotations

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
}

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

    The base of the division (part before the first ``_``) is looked up in
    ``_DIVISION_ROLE_MAP``; if absent, the base itself is used as the role.

    Args:
        division: Subdirectory name from the Agency repo (e.g. ``"engineering"``,
            ``"qa_testing"``).

    Returns:
        Bernstein role string.
    """
    base = division.split("_")[0]
    return _DIVISION_ROLE_MAP.get(base, base)


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

        description: str = str(fm.get("description") or "")
        role = _division_to_role(division)
        agent_id = f"agency:{_slugify(name)}"

        # Extract capabilities list (e.g. [api-design, authentication, jwt])
        raw_caps: list[Any] = list(fm.get("capabilities") or [])
        capabilities: list[str] = [str(c) for c in raw_caps]

        # Extract preferred tools list (e.g. [pytest, ruff, mypy])
        raw_tools: list[Any] = list(fm.get("tools") or [])
        tools: list[str] = [str(t) for t in raw_tools]

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
        if not self.is_available():
            return []

        agents: list[CatalogAgent] = []
        for division_dir in sorted(self._local_path.iterdir()):
            if not division_dir.is_dir():
                continue
            division = division_dir.name
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
                timeout=120,
            )
            if result.returncode != 0:
                return False, f"git clone failed: {result.stderr.strip()}"
            action = "cloned"

        marker.touch()
        return True, f"Agency catalog {action} from {url}"
