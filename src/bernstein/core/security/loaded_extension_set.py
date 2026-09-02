"""Content-addressed record of the skills and plugins an install *loaded*.

``bernstein skills`` and ``bernstein plugins`` answer "what does this install
declare?". That is the cheaper of the two questions. What an agent could
actually do in a given run is decided later, by resolution: a path override
moves a pack, an entry point resolves to a different distribution than the
lockfile names, and an import that raises leaves the surface narrower while
the declaration still lists the entry. Resolution failures are logged and
the run continues, so nothing downstream can tell "loaded" from "declared
and gone".

This module records the resolved set at the point resolution completes:

* :class:`LoadedExtension` - one entry per skill or plugin, carrying the
  source it resolved through, its resolved version, the origin the bytes
  actually came from, and a SHA-256 digest of those bytes. A declared entry
  that failed to load is present with ``loaded=False`` and the error text,
  never absent.
* :class:`LoadedExtensionSet` - the whole set, content-addressed the way
  :mod:`bernstein.core.security.sbom` addresses a component inventory: the
  digest is a pure function of the canonical JSON of every entry, so two
  installs that resolved the same bytes produce the same digest and a
  single changed plugin file changes it.
* :func:`record_loaded_extension_set` - appends the set to the run's
  Merkle-chained journal, which is what makes the run receipt able to name
  it: :mod:`bernstein.core.replay.run_receipt` *recomputes* the set digest
  from the embedded rows and binds it into the signed subject.

Digests cover bytes, not declarations. For a skill it is the body the
loader holds after sanitisation - the text an agent would actually be
served. For a plugin it is the module file the registered object resolved
from, read at record time, so a plugin edited between two runs records two
different digests.

Origins are recorded as resolved, never normalised back to the declared
root: a pack symlinked into ``templates/skills`` records the real path it
resolves to, because the point of the record is to show where the bytes
came from. Deciding whether that origin is *acceptable* is admission's
job (#4907), not this module's.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.skills.loader import SkillLoader
    from bernstein.plugins.manager import PluginManager

#: Record schema version. Bump only on a wire-format change.
LOADED_EXTENSION_SET_SCHEMA_VERSION: str = "1.0.0"

#: Journal event type carrying the resolved set for one run.
LOADED_EXTENSION_SET_EVENT: str = "loaded_extension_set"

_DIGEST_PREFIX = "sha256:"


class ExtensionKind(StrEnum):
    """What kind of extension an entry describes."""

    SKILL = "skill"
    PLUGIN = "plugin"


def content_digest(data: bytes) -> str:
    """Return the prefixed SHA-256 digest of *data*.

    The ``sha256:`` prefix is carried so a future algorithm change is a
    readable migration rather than a silent reinterpretation of 64 hex
    characters.
    """
    return _DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class LoadedExtension:
    """One skill or plugin as resolution left it.

    Attributes:
        kind: Skill or plugin.
        name: Resolved name (skill name, plugin registration name).
        source: The source it resolved through - a skill source label, an
            entry-point group, or ``config`` for a ``bernstein.yaml`` entry.
        origin: Where the bytes came from: the resolved filesystem path when
            one exists, otherwise the declared import target. Never rewritten
            back to the declared root.
        version: Resolved version - the manifest version for a skill, the
            distribution version for a plugin. Empty when unknowable.
        content_digest: :func:`content_digest` over the loaded bytes. Empty
            for an entry that did not load, which has no bytes to address.
        loaded: Whether the entry is actually available in this process.
        failure: Error text for an entry that did not load, else empty.
    """

    kind: ExtensionKind
    name: str
    source: str
    origin: str
    version: str
    content_digest: str
    loaded: bool
    failure: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical dict form (all keys always present)."""
        return {
            "content_digest": self.content_digest,
            "failure": self.failure,
            "kind": str(self.kind),
            "loaded": self.loaded,
            "name": self.name,
            "origin": self.origin,
            "source": self.source,
            "version": self.version,
        }

    def sort_key(self) -> tuple[str, str, str, str]:
        """Deterministic ordering key: kind, then name, then source, then origin."""
        return (str(self.kind), self.name, self.source, self.origin)


@dataclass(frozen=True, slots=True)
class LoadedExtensionSet:
    """Every resolved entry, content-addressed as one set."""

    entries: tuple[LoadedExtension, ...]

    @property
    def digest(self) -> str:
        """Content address of the whole set.

        A pure function of the canonical JSON of every entry, so it is
        recomputable from the recorded rows alone - no counter is stored
        and none is trusted.
        """
        return content_digest(self.to_canonical_bytes())

    def to_canonical_bytes(self) -> bytes:
        """Return the deterministic JSON bytes the digest is taken over."""
        return digest_preimage([entry.to_dict() for entry in self.entries])

    def to_dict(self) -> dict[str, Any]:
        """Return the serialisable record, digest included."""
        return {
            "schema_version": LOADED_EXTENSION_SET_SCHEMA_VERSION,
            "digest": self.digest,
            "entry_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def loaded(self) -> tuple[LoadedExtension, ...]:
        """Entries that are actually available in this process."""
        return tuple(entry for entry in self.entries if entry.loaded)

    def not_loaded(self) -> tuple[LoadedExtension, ...]:
        """Declared entries that resolution could not produce."""
        return tuple(entry for entry in self.entries if not entry.loaded)


def digest_preimage(entry_dicts: list[dict[str, Any]]) -> bytes:
    """Return the canonical bytes a set digest is computed over.

    Exposed so a verifier holding only the recorded rows - the run receipt,
    for instance - recomputes the same digest without rebuilding the
    dataclasses.
    """
    payload = {
        "schema_version": LOADED_EXTENSION_SET_SCHEMA_VERSION,
        "entries": entry_dicts,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Projection from the live resolution surfaces
# ---------------------------------------------------------------------------


def _resolved_origin(origin: str) -> str:
    """Return the real path *origin* names, or *origin* itself when it is not a path.

    ``resolve()`` follows symlinks deliberately: a pack linked into the
    declared root came from wherever the link points, and recording the link
    path would hide exactly the substitution the record exists to show.
    """
    if not origin:
        return ""
    candidate = Path(origin)
    try:
        if candidate.exists():
            return str(candidate.resolve())
    except OSError:
        return origin
    return origin


def skill_entries(loader: SkillLoader) -> list[LoadedExtension]:
    """Project a :class:`~bernstein.core.skills.loader.SkillLoader` onto entries.

    Loaded skills are digested over the body the loader holds. Skill-source
    entry points that failed to import contribute one not-loaded entry each;
    the ones that imported are represented by the skills they produced.
    """
    entries: list[LoadedExtension] = []
    for skill in loader.list_all():
        entries.append(
            LoadedExtension(
                kind=ExtensionKind.SKILL,
                name=skill.name,
                source=skill.source_name,
                origin=_resolved_origin(skill.origin),
                version=skill.version,
                content_digest=content_digest(skill.body.encode("utf-8")),
                loaded=True,
                failure="",
            )
        )
    for resolution in loader.source_resolutions:
        if resolution.loaded:
            continue
        entries.append(
            LoadedExtension(
                kind=ExtensionKind.SKILL,
                name=resolution.name,
                source=resolution.source,
                origin=resolution.declared,
                version=resolution.version,
                content_digest="",
                loaded=False,
                failure=resolution.failure,
            )
        )
    return entries


def plugin_entries(plugin_manager: PluginManager) -> list[LoadedExtension]:
    """Project a :class:`~bernstein.plugins.manager.PluginManager` onto entries.

    A loaded plugin is digested over the module file its registered object
    resolved from, read now rather than cached at import, so an edit between
    two runs shows up as a different digest.
    """
    entries: list[LoadedExtension] = []
    for resolution in plugin_manager.resolutions:
        origin = _resolved_origin(resolution.origin) if resolution.loaded else resolution.declared
        digest = ""
        if resolution.loaded and origin:
            try:
                digest = content_digest(Path(origin).read_bytes())
            except OSError:
                digest = ""
        entries.append(
            LoadedExtension(
                kind=ExtensionKind.PLUGIN,
                name=resolution.name,
                source=resolution.source,
                origin=origin,
                version=resolution.version,
                content_digest=digest,
                loaded=resolution.loaded,
                failure=resolution.failure,
            )
        )
    return entries


def build_loaded_extension_set(
    *,
    loader: SkillLoader | None = None,
    plugin_manager: PluginManager | None = None,
) -> LoadedExtensionSet:
    """Build the resolved set from whichever surfaces are available.

    Args:
        loader: The skill loader whose resolution has completed.
        plugin_manager: The plugin manager whose discovery has completed.

    Returns:
        A :class:`LoadedExtensionSet` with entries in deterministic order.
    """
    entries: list[LoadedExtension] = []
    if loader is not None:
        entries.extend(skill_entries(loader))
    if plugin_manager is not None:
        entries.extend(plugin_entries(plugin_manager))
    entries.sort(key=lambda entry: entry.sort_key())
    return LoadedExtensionSet(entries=tuple(entries))


# ---------------------------------------------------------------------------
# Journal binding
# ---------------------------------------------------------------------------


def record_loaded_extension_set(journal: EventJournal, extension_set: LoadedExtensionSet) -> None:
    """Append the resolved set to a run journal as a Merkle-chained event.

    The asserted digest is carried for readability; every consumer
    recomputes it from the embedded rows via
    :func:`extension_set_digest_from_events`, so a row edited after the
    fact is caught by the chain rather than accepted by a matching header.
    """
    journal.record(
        LOADED_EXTENSION_SET_EVENT,
        extension_set_digest=extension_set.digest,
        extension_set_schema_version=LOADED_EXTENSION_SET_SCHEMA_VERSION,
        extension_count=len(extension_set.entries),
        extensions=[entry.to_dict() for entry in extension_set.entries],
    )


def record_run_extension_set(journal: EventJournal, workdir: Path) -> LoadedExtensionSet:
    """Resolve *workdir*'s skills and plugins and append the set to *journal*.

    This is the run-start entry point: it asks the two resolution surfaces
    what they ended up with - the skill loader for ``templates/skills`` plus
    any ``bernstein.skill_sources`` entry points, and the process plugin
    manager for entry-point, config and reporter plugins - and records the
    result once per run, before any agent is spawned.

    Both surfaces are the same ones the run itself uses (the loader comes
    from the role resolver's cache, the manager is the process singleton),
    so the recorded set is what the run served, not a second resolution
    that might differ.

    Args:
        journal: The run's Merkle-chained journal.
        workdir: Project root the run is executing against.

    Returns:
        The set that was recorded.
    """
    from bernstein import get_templates_dir
    from bernstein.core.planning.role_resolver import get_loader
    from bernstein.plugins.manager import get_plugin_manager

    extension_set = build_loaded_extension_set(
        loader=get_loader(get_templates_dir(workdir) / "roles"),
        plugin_manager=get_plugin_manager(workdir),
    )
    record_loaded_extension_set(journal, extension_set)
    return extension_set


def extension_set_digest_from_events(events: list[dict[str, Any]]) -> str | None:
    """Recompute the resolved-set digest from journal rows, or ``None``.

    The most recent :data:`LOADED_EXTENSION_SET_EVENT` row wins - a run that
    reloads its extensions is attested by the set it ended with. The digest
    is recomputed from the embedded ``extensions`` array; the row's own
    ``extension_set_digest`` field is never read.
    """
    for event in reversed(events):
        if event.get("event") != LOADED_EXTENSION_SET_EVENT:
            continue
        raw = event.get("extensions")
        if not isinstance(raw, list):
            continue
        items = cast("list[Any]", raw)
        rows: list[dict[str, Any]] = [cast("dict[str, Any]", row) for row in items if isinstance(row, dict)]
        if len(rows) != len(items):
            continue
        return content_digest(digest_preimage(rows))
    return None
