"""Reads agent catalogs laid out as a Claude Code plugin/subagent tree.

Three on-disk shapes feed the same reader:

- Standalone ``.claude/agents/*.md`` files.
- ``plugins/<name>/agents/<agent>.md`` files under each plugin directory.
- An optional ``.claude-plugin/marketplace.json`` index that, when present
  and valid, scopes plugin discovery to the plugins it lists; absent or
  malformed, discovery falls back to scanning every ``plugins/*`` directory
  so one broken index cannot blind the loader to agents on disk.

Every file is YAML frontmatter (``name``, ``description``, optional
``model``, optional ``tools``) followed by a Markdown body used verbatim as
the system prompt. Frontmatter that fails to parse - missing fence, invalid
YAML, missing required field, wrong field type - is never silently dropped:
it comes back as a named :class:`AgentDefinitionError` the caller can log or
surface, alongside whatever else in the same catalog parsed cleanly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bernstein.agents.catalog import CatalogAgent

logger = logging.getLogger(__name__)

_MARKETPLACE_RELATIVE_PATH = Path(".claude-plugin") / "marketplace.json"
_PLUGINS_DIRNAME = "plugins"
_PLUGIN_AGENTS_SUBDIR = "agents"
_STANDALONE_AGENTS_RELATIVE_PATH = Path(".claude") / "agents"

# Keyword signals scored against the agent's name + description to infer a
# Bernstein role. The plugin frontmatter has no role/division concept (only
# name/description/model/tools), so this is the only signal available.
# Order matters: the first matching role wins, so more specific roles are
# listed before their broader neighbours.
_ROLE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security", ("security", "vulnerab", "threat", "pentest")),
    ("reviewer", ("review",)),
    ("qa", ("test", "qa", "quality")),
    ("devops", ("devops", "infra", "deploy", "sre ", "site reliability")),
    ("frontend", ("frontend", "front-end", "ui ", "ux ")),
    ("backend", ("backend", "back-end", "api ", "database")),
    ("architect", ("architect",)),
    ("docs", ("documentation", "technical writ")),
    ("manager", ("project manage", "product manage", "sprint")),
    ("ml-engineer", ("machine learning", "ml engineer", "data scien")),
)
_DEFAULT_ROLE = "backend"


@dataclass(frozen=True)
class AgentDefinitionError:
    """A single agent-definition (or index) file that failed to parse.

    Attributes:
        path: File that failed to parse.
        reason: Human-readable cause, safe to log or display verbatim.
    """

    path: str
    reason: str


@dataclass(frozen=True)
class PluginCatalogResult:
    """Outcome of reading a plugin-layout catalog root.

    Attributes:
        agents: Successfully parsed CatalogAgent records.
        errors: Named errors for definitions that failed to parse. Present
            alongside ``agents`` rather than instead of them - a malformed
            file never silently reduces the count with no trace of why.
    """

    agents: list[CatalogAgent] = field(default_factory=list[CatalogAgent])
    errors: list[AgentDefinitionError] = field(default_factory=list[AgentDefinitionError])


def _slugify(name: str) -> str:
    """Return a URL-safe slug for *name*."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def _infer_role(name: str, description: str) -> str:
    """Infer a Bernstein role from an agent's name and description.

    The plugin frontmatter carries no role field, so this is a best-effort
    keyword match against the canonical Bernstein role vocabulary. Agents
    that match nothing fall back to :data:`_DEFAULT_ROLE` rather than an
    unmatchable made-up role string.
    """
    haystack = f"{name} {description}".lower()
    for role, keywords in _ROLE_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return role
    return _DEFAULT_ROLE


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return ``(frontmatter_text, body)``, or ``None`` if there is no fence."""
    if not text.startswith("---"):
        return None
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return None
    return rest[:end], rest[end + 4 :].lstrip("\n")


def parse_agent_file(path: Path) -> CatalogAgent | AgentDefinitionError:
    """Parse one agent-definition Markdown file into a ``CatalogAgent``.

    Extracts ``name`` and ``description`` (required), ``model`` and
    ``tools`` (optional) from the YAML frontmatter; the body after the
    frontmatter fence becomes ``system_prompt`` verbatim.

    Args:
        path: Path to a ``.md`` agent-definition file.

    Returns:
        A ``CatalogAgent`` on success, or an :class:`AgentDefinitionError`
        naming *path* and the reason it could not be parsed. Malformed
        input is never represented as a bare ``None`` or an empty list -
        the caller always has something to log or report.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return AgentDefinitionError(path=str(path), reason=f"cannot read file: {exc}")

    split = _split_frontmatter(text)
    if split is None:
        return AgentDefinitionError(path=str(path), reason="missing or unterminated YAML frontmatter fence ('---')")
    fm_text, body = split

    try:
        fm: Any = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return AgentDefinitionError(path=str(path), reason=f"invalid YAML frontmatter: {exc}")

    if not isinstance(fm, dict):
        return AgentDefinitionError(path=str(path), reason="frontmatter must be a mapping")

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        return AgentDefinitionError(path=str(path), reason="missing required field 'name'")

    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        return AgentDefinitionError(path=str(path), reason="missing required field 'description'")

    model = fm.get("model")
    if model is not None and not isinstance(model, str):
        return AgentDefinitionError(path=str(path), reason="'model' must be a string")

    raw_tools = fm.get("tools")
    tools: list[str] = []
    if raw_tools is not None:
        if not isinstance(raw_tools, list) or not all(isinstance(t, str) for t in raw_tools):
            return AgentDefinitionError(path=str(path), reason="'tools' must be a list of strings")
        tools = list(raw_tools)

    if not body.strip():
        return AgentDefinitionError(path=str(path), reason="empty system prompt body")

    clean_name = name.strip()
    clean_description = description.strip()
    return CatalogAgent(
        name=clean_name,
        role=_infer_role(clean_name, clean_description),
        description=clean_description,
        system_prompt=body,
        id=f"plugin:{_slugify(clean_name)}",
        tools=tools,
        capabilities=[],
        priority=100,
        source="plugin",
        model=model or "",
    )


def _load_marketplace_index(path: Path) -> tuple[list[str] | None, AgentDefinitionError | None]:
    """Parse a ``.claude-plugin/marketplace.json`` index.

    Returns:
        ``(plugin_names, error)``. *plugin_names* is ``None`` when the file
        is absent (nothing to scope by) or malformed (caller falls back to
        scanning every plugin directory rather than trusting a broken
        index to mean "no plugins"). *error* is set only for the malformed
        case, so a broken index is reported rather than silently ignored.
    """
    if not path.is_file():
        return None, None

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, AgentDefinitionError(path=str(path), reason=f"cannot read file: {exc}")

    try:
        data: Any = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, AgentDefinitionError(path=str(path), reason=f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return None, AgentDefinitionError(path=str(path), reason="marketplace.json must be a JSON object")

    raw_plugins = data.get("plugins")
    if not isinstance(raw_plugins, list):
        return None, AgentDefinitionError(path=str(path), reason="marketplace.json 'plugins' must be a list")

    names: list[str] = []
    for index, entry in enumerate(raw_plugins):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            return None, AgentDefinitionError(
                path=str(path),
                reason=f"marketplace.json 'plugins[{index}]' missing a string 'name' field",
            )
        names.append(entry["name"])
    return names, None


def _collect(md_file: Path, agents: list[CatalogAgent], errors: list[AgentDefinitionError]) -> None:
    """Parse *md_file* and append the result onto the matching list."""
    parsed = parse_agent_file(md_file)
    if isinstance(parsed, CatalogAgent):
        agents.append(parsed)
    else:
        errors.append(parsed)
        logger.warning("Skipping malformed agent definition %s: %s", parsed.path, parsed.reason)


def load_plugin_catalog(root: Path) -> PluginCatalogResult:
    """Read every agent definition under the plugin/subagent layout at *root*.

    Args:
        root: Directory that may contain ``.claude/agents/``,
            ``plugins/*/agents/``, and/or ``.claude-plugin/marketplace.json``.
            Any subset may be absent; an empty result is not an error.

    Returns:
        A :class:`PluginCatalogResult` with every successfully parsed agent
        and every named parse failure encountered along the way.
    """
    agents: list[CatalogAgent] = []
    errors: list[AgentDefinitionError] = []

    standalone_dir = root / _STANDALONE_AGENTS_RELATIVE_PATH
    if standalone_dir.is_dir():
        for md_file in sorted(standalone_dir.glob("*.md")):
            _collect(md_file, agents, errors)

    plugins_dir = root / _PLUGINS_DIRNAME
    if plugins_dir.is_dir():
        allowed_names, index_error = _load_marketplace_index(root / _MARKETPLACE_RELATIVE_PATH)
        if index_error is not None:
            errors.append(index_error)
            logger.warning("Skipping malformed marketplace index %s: %s", index_error.path, index_error.reason)

        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            if allowed_names is not None and plugin_dir.name not in allowed_names:
                continue
            plugin_agents_dir = plugin_dir / _PLUGIN_AGENTS_SUBDIR
            if not plugin_agents_dir.is_dir():
                continue
            for md_file in sorted(plugin_agents_dir.glob("*.md")):
                _collect(md_file, agents, errors)

    return PluginCatalogResult(agents=agents, errors=errors)


__all__ = [
    "AgentDefinitionError",
    "PluginCatalogResult",
    "load_plugin_catalog",
    "parse_agent_file",
]
