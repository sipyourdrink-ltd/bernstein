"""Load Agency agent personas as additional Bernstein role templates.

The Agency project (https://github.com/cagostino/agency) provides 141+
specialized agent personas organised by division.  This module parses
those persona files and exposes them as a catalog that the Spawner can
query when a task's role is not covered by templates/roles/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

# Agency division → Bernstein role name mapping.
# Divisions without a direct mapping fall through as-is (lowercased).
_DIVISION_TO_ROLE: dict[str, str] = {
    "engineering": "backend",
    "software_engineering": "backend",
    "frontend_engineering": "frontend",
    "qa_testing": "qa",
    "quality_assurance": "qa",
    "security": "security",
    "cybersecurity": "security",
    "devops": "devops",
    "infrastructure": "devops",
    "documentation": "docs",
    "technical_writing": "docs",
    "architecture": "architect",
    "design": "architect",
    "machine_learning": "ml-engineer",
    "data_science": "ml-engineer",
    "management": "manager",
    "product": "manager",
    "review": "reviewer",
    "code_review": "reviewer",
}


@dataclass(frozen=True)
class AgencyAgent:
    """A parsed Agency agent persona.

    Attributes:
        name: Unique agent identifier (filename stem).
        description: Short one-line description.
        division: Agency division the agent belongs to.
        role: Mapped Bernstein role name.
        prompt_body: Full system-prompt text for the agent.
    """

    name: str
    description: str
    division: str
    role: str
    prompt_body: str


def _map_division(division: str) -> str:
    """Map an Agency division name to a Bernstein role.

    Args:
        division: Raw division string from the Agency YAML.

    Returns:
        Bernstein role name.
    """
    key = division.strip().lower().replace(" ", "_").replace("-", "_")
    return _DIVISION_TO_ROLE.get(key, key)


def parse_agency_agent(path: Path) -> AgencyAgent:
    """Parse a single Agency persona YAML file.

    Expected YAML structure (at minimum)::

        name: "persona-name"
        description: "One-line description"
        division: "Engineering"
        system_prompt: |
          You are a ...

    Args:
        path: Path to a ``.yaml`` or ``.yml`` persona file.

    Returns:
        Parsed AgencyAgent dataclass.

    Raises:
        ValueError: If required fields are missing or the file is not valid YAML.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read agency file {path}: {exc}") from exc

    try:
        raw_data: object = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise ValueError(f"Agency file must be a YAML mapping: {path}")

    data: dict[str, Any] = cast("dict[str, Any]", raw_data)

    name_val: Any = data.get("name")
    if not name_val or not isinstance(name_val, str):
        raise ValueError(f"Agency file missing 'name': {path}")
    name: str = name_val

    description: str = str(data.get("description", ""))
    division: str = str(data.get("division", "general"))
    role = _map_division(division)

    # Accept either 'system_prompt' or 'prompt' as the body field.
    prompt_body: str = str(data.get("system_prompt") or data.get("prompt") or "")

    return AgencyAgent(
        name=name,
        description=description,
        division=division,
        role=role,
        prompt_body=prompt_body,
    )


def load_agency_catalog(catalog_dir: Path) -> dict[str, AgencyAgent]:
    """Load all Agency persona YAML files from a directory.

    Scans ``catalog_dir`` (non-recursively) for ``.yaml`` / ``.yml`` files
    and parses each one.  Files that fail to parse are logged and skipped.

    Args:
        catalog_dir: Directory containing Agency persona YAML files.

    Returns:
        Mapping of agent *name* → ``AgencyAgent``.  Empty dict if the
        directory does not exist or contains no valid files.
    """
    if not catalog_dir.is_dir():
        log.warning("Agency catalog directory does not exist: %s", catalog_dir)
        return {}

    catalog: dict[str, AgencyAgent] = {}
    for p in sorted(catalog_dir.iterdir()):
        if p.suffix not in (".yaml", ".yml"):
            continue
        try:
            agent = parse_agency_agent(p)
            catalog[agent.name] = agent
        except ValueError:
            log.warning("Skipping invalid agency file: %s", p, exc_info=True)

    log.info("Loaded %d agency agents from %s", len(catalog), catalog_dir)
    return catalog
