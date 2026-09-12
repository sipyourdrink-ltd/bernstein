"""A probe is a declared record, not a function name in a dispatch table.

Which attribute a discovery pass collects, how it collects it, how long it may
take and what it is allowed to assert were all implicit in code:
``agent_discovery._RICH_DETECTOR_NAMES`` named functions, and nothing anywhere
carried a refresh interval, a timeout, a cost class or a taint tag. So adding
one attribute to what a probe reports was a code change and a release, and two
operators could not compare two runs without diffing the source that produced
them (#5081, slice 1).

A probe declared here is data. Two operators running the same probe-set version
against the same targets get the same declared surface, and the review surface
for a new probe is the probe file rather than a diff to a dispatch function
that nine other probes also run through.

**Unknown fields round-trip unchanged.** A probe file written by a newer build
carries fields this one does not interpret, and dropping them on load would
silently rewrite an operator's file the next time anything saved it. They are
preserved verbatim and re-emitted in :meth:`Probe.to_dict`, so an older build
is a reader that does not understand every field rather than one that destroys
it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "CollectionMethod",
    "CostClass",
    "Probe",
    "ProbeError",
    "ProbeSet",
    "load_probe_set",
]


class ProbeError(ValueError):
    """A probe declaration that cannot be read as one.

    Raised at load, never at use: a probe set is operator-authored data, and a
    file that cannot be interpreted must fail where it is read rather than
    halfway through a discovery pass.
    """


class CollectionMethod(StrEnum):
    """How a probe obtains its attribute."""

    #: Run a command and read its output.
    COMMAND = "command"
    #: Read a file from the target's filesystem.
    FILE = "file"
    #: Ask an API endpoint.
    API = "api"
    #: Derive from something already collected; no I/O of its own.
    DERIVED = "derived"


class CostClass(StrEnum):
    """What a probe costs to run, so a scheduler can budget without timing it."""

    #: Local, sub-second, no network.
    FREE = "free"
    #: Local but slow, or one cheap network call.
    CHEAP = "cheap"
    #: Several network calls, or a metered API.
    EXPENSIVE = "expensive"


#: Keys :class:`Probe` interprets. Anything else in a declaration is carried
#: through untouched — see the module docstring.
_KNOWN_KEYS = frozenset(
    {
        "id",
        "attribute",
        "collection_method",
        "refresh_interval_s",
        "timeout_s",
        "cost_class",
        "taint_tags",
    }
)


@dataclass(frozen=True, slots=True)
class Probe:
    """One declared probe.

    Attributes:
        id: Stable identifier. Two runs of the same id are comparable; it is
            what a journal entry names, so it may not change when a probe's
            implementation does.
        attribute: The attribute this probe produces.
        collection_method: How it is obtained.
        refresh_interval_s: How long a result stays usable. ``0`` means every
            pass re-collects.
        timeout_s: Hard ceiling for one invocation. A probe with no ceiling is
            a probe that can hang a whole pass, so this is required.
        cost_class: What running it costs.
        taint_tags: Tags this probe may assert on its result, and the only ones
            it may. A probe that could assert anything is not a declaration.
        unknown: Fields this build does not interpret, preserved verbatim.
    """

    id: str
    attribute: str
    collection_method: CollectionMethod
    timeout_s: float
    refresh_interval_s: float = 0.0
    cost_class: CostClass = CostClass.CHEAP
    taint_tags: tuple[str, ...] = ()
    unknown: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, unknown fields included.

        Known keys are emitted in a fixed order so two writers of the same
        probe produce the same bytes; unknown keys follow, sorted, for the
        same reason.
        """
        out: dict[str, Any] = {
            "id": self.id,
            "attribute": self.attribute,
            "collection_method": str(self.collection_method),
            "refresh_interval_s": self.refresh_interval_s,
            "timeout_s": self.timeout_s,
            "cost_class": str(self.cost_class),
            "taint_tags": list(self.taint_tags),
        }
        for key in sorted(self.unknown):
            out[key] = self.unknown[key]
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, origin: str = "<memory>") -> Probe:
        """Rebuild a probe from a declaration.

        Args:
            raw: The declaration.
            origin: Where it came from, named in any refusal so an operator
                does not have to guess which file is wrong.

        Raises:
            ProbeError: A required field is missing or uninterpretable.
        """
        probe_id = _require_text(raw, "id", origin)
        attribute = _require_text(raw, "attribute", origin)
        method = _require_member(raw, "collection_method", CollectionMethod, origin, probe_id)
        cost = (
            _require_member(raw, "cost_class", CostClass, origin, probe_id)
            if raw.get("cost_class") is not None
            else CostClass.CHEAP
        )
        timeout = _require_positive(raw, "timeout_s", origin, probe_id)
        refresh = _optional_non_negative(raw, "refresh_interval_s", origin, probe_id)
        tags = raw.get("taint_tags", ())
        if not isinstance(tags, list | tuple) or any(not isinstance(t, str) for t in tags):
            raise ProbeError(f"{origin}: probe {probe_id!r} taint_tags must be a list of strings")
        return cls(
            id=probe_id,
            attribute=attribute,
            collection_method=method,
            timeout_s=timeout,
            refresh_interval_s=refresh,
            cost_class=cost,
            taint_tags=tuple(str(t) for t in tags),
            unknown={k: v for k, v in raw.items() if k not in _KNOWN_KEYS},
        )


@dataclass(frozen=True, slots=True)
class ProbeSet:
    """Every probe an operator declared, and the version they declared it at.

    ``version`` is what a run records so "which probe set produced this" is
    answerable from the record alone. It is the directory's own declaration,
    not a hash of the files: an operator who edits a probe and does not bump it
    is making a statement, and inventing a version would hide that.
    """

    version: str
    probes: tuple[Probe, ...]

    def __iter__(self) -> Iterator[Probe]:
        return iter(self.probes)

    def __len__(self) -> int:
        return len(self.probes)

    def by_id(self, probe_id: str) -> Probe | None:
        """Return the probe with *probe_id*, or None."""
        return next((p for p in self.probes if p.id == probe_id), None)


def load_probe_set(directory: Path, *, version: str = "") -> ProbeSet:
    """Load every ``*.json`` probe declaration under *directory*.

    Files are read in sorted name order so the set is the same on every
    machine. A directory that does not exist is an empty set, not an error:
    an operator who has declared no probes has declared no probes.

    Args:
        directory: Directory holding probe declarations.
        version: The probe-set version to record. Defaults to the value in
            ``probe-set.json`` if present, else "".

    Raises:
        ProbeError: A file is unparsable, or two probes share an id.
    """
    if not directory.is_dir():
        return ProbeSet(version=version, probes=())

    declared_version = version
    manifest = directory / "probe-set.json"
    if not declared_version and manifest.is_file():
        declared_version = str(_read_json(manifest).get("version", ""))

    probes: list[Probe] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "probe-set.json":
            continue
        raw = _read_json(path)
        probe = Probe.from_dict(raw, origin=str(path))
        if probe.id in seen:
            # Two declarations of one id make "which probe produced this"
            # unanswerable from the record, which is the whole point of the id.
            raise ProbeError(f"{path}: probe id {probe.id!r} is already declared by {seen[probe.id]}")
        seen[probe.id] = str(path)
        probes.append(probe)
    return ProbeSet(version=declared_version, probes=tuple(probes))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"{path}: cannot be read as JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProbeError(f"{path}: a probe declaration must be an object")
    return dict(loaded)


def _require_text(raw: dict[str, Any], key: str, origin: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"{origin}: {key} must be a non-empty string")
    return value


def _require_member(raw: dict[str, Any], key: str, enum: type[StrEnum], origin: str, probe_id: str) -> Any:
    value = raw.get(key)
    try:
        return enum(str(value))
    except ValueError as exc:
        allowed = ", ".join(sorted(str(m) for m in enum))
        raise ProbeError(f"{origin}: probe {probe_id!r} {key}={value!r} is not one of: {allowed}") from exc


def _require_positive(raw: dict[str, Any], key: str, origin: str, probe_id: str) -> float:
    value = raw.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ProbeError(f"{origin}: probe {probe_id!r} {key} must be a positive number")
    return float(value)


def _optional_non_negative(raw: dict[str, Any], key: str, origin: str, probe_id: str) -> float:
    value = raw.get(key, 0.0)
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise ProbeError(f"{origin}: probe {probe_id!r} {key} must be a non-negative number")
    return float(value)
