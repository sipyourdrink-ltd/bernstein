"""Install-wide pin manifest for plugins and skills (issue #5089).

``plugin_reconciler`` removes what a marketplace stopped listing; the
catalog lockfile records what *was* installed. Neither answers the
governing question an operator actually has: *is what this install loads
exactly what it was allowed to load?* This module adds the missing
document -- one version-controlled allow-list naming every plugin and
skill, each at an exact version and content address, plus the sources
each environment may load them from.

Three properties make the manifest worth having:

* **No floating pins.** A specifier like ``latest``, ``^1.2.0`` or ``1.2``
  is rejected when the manifest is parsed, not warned about at load time.
  The rejection mirrors
  :func:`~bernstein.core.plugins_core.plugin_manifest._validate_semver`,
  raised to the install-wide level: the manifest names an exact byte, so
  an install can never silently drift onto whatever ``latest`` resolves to
  on a given day.

* **Divergence is enumerable.** :func:`verify_pinned_set` compares a
  resolved set against the manifest and returns one
  :class:`PinDrift` per divergence in presence, version, content hash, or
  source -- so a caller can print every drifted entry rather than the
  first one.

* **Applying is idempotent and recorded.** :func:`apply_pin_manifest`
  writes the manifest's canonical bytes to
  ``.sdd/plugins/pins/applied.json`` only when they differ, and appends a
  :class:`PinApplyRecord` carrying the manifest hash before and after to
  ``.sdd/plugins/pins/decisions.jsonl`` on *every* apply, no-ops included.

Scope: this module does not resolve the loaded set itself. Callers pass
the resolved plugins and skills in as :class:`LoadedComponent` values.

One manifest, not two
---------------------

Plugins and skills live in separate subsystems with separate lockfiles,
but the allow-list is deliberately a single document covering both. The
question it answers is install-wide, so it needs a single answer and a
single hash: a decision record that says "the install moved from manifest
hash A to hash B" is only meaningful if one hash covers everything the
install may load. Two parallel files would give two hashes, two apply
ledgers, and no single value to compare.

Usage::

    from bernstein.core.plugins_core.plugin_pin_manifest import (
        load_pin_manifest,
        verify_pinned_set,
    )

    manifest = load_pin_manifest(workdir / ".bernstein" / "pins.yaml")
    result = verify_pinned_set(manifest, resolved, environment="production")
    if not result.ok:
        for line in result.render_lines():
            print(line)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.plugins_core.plugin_manifest import is_exact_semver

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Component kind for an entry under the manifest's ``plugins:`` key.
PIN_KIND_PLUGIN = "plugin"

#: Component kind for an entry under the manifest's ``skills:`` key.
PIN_KIND_SKILL = "skill"

#: The two component kinds, in the order they are read from the manifest.
PIN_KINDS: tuple[str, str] = (PIN_KIND_PLUGIN, PIN_KIND_SKILL)

#: Manifest schema version this parser understands.
PIN_MANIFEST_VERSION = 1

#: Path of the applied-state file and the apply ledger, under the project root.
PIN_STATE_SUBPATH: tuple[str, ...] = (".sdd", "plugins", "pins")

#: Filename of the canonical applied manifest state.
PIN_APPLIED_FILENAME = "applied.json"

#: Filename of the append-only apply ledger.
PIN_DECISIONS_FILENAME = "decisions.jsonl"

#: Exit code :func:`PinVerifyResult.exit_code` returns when anything drifted.
PIN_VERIFY_DRIFT_EXIT = 2

#: Component names: alphanumeric plus hyphen, underscore and dot.
_PIN_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

#: A pinned content address is a full SHA-256 digest, never a prefix.
_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Reasons a :class:`PinDrift` can carry, in the order they are checked.
DRIFT_ENVIRONMENT = "environment"
DRIFT_UNPINNED = "unpinned"
DRIFT_ABSENT = "absent"
DRIFT_VERSION = "version"
DRIFT_CONTENT_HASH = "content_hash"
DRIFT_SOURCE = "source"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PinManifestError(Exception):
    """Raised when a pin manifest fails to parse or validate.

    Attributes:
        errors: Every individual validation failure, so an operator fixing
            the manifest sees all of them in one pass.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(
            f"Pin manifest validation failed ({len(errors)} error(s)):\n" + "\n".join(f"  - {e}" for e in errors),
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinEntry:
    """One pinned plugin or skill.

    Attributes:
        kind: :data:`PIN_KIND_PLUGIN` or :data:`PIN_KIND_SKILL`.
        name: Component name, unique within its kind.
        version: Exact ``MAJOR.MINOR.PATCH`` version; never a range.
        content_hash: Content address (``sha256:<64 hex>``) of the tree the
            install is allowed to load.
        source: Marketplace or repository the component may come from.
    """

    kind: str
    name: str
    version: str
    content_hash: str
    source: str

    def to_dict(self) -> dict[str, str]:
        """Serialise to a JSON-friendly dict."""
        return {
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "content_hash": self.content_hash,
            "source": self.source,
        }


@dataclass(frozen=True)
class PinEnvironment:
    """The sources one environment may load pinned components from.

    Attributes:
        name: Environment name, e.g. ``production``.
        allowed_sources: Sources permitted in this environment. A component
            from any other source is rejected by :func:`verify_pinned_set`
            regardless of its version or content hash.
    """

    name: str
    allowed_sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {"name": self.name, "allowed_sources": list(self.allowed_sources)}


@dataclass(frozen=True)
class PinManifest:
    """The parsed install-wide allow-list.

    Entries and environments are stored sorted, so two manifests listing
    the same pins in a different file order share one manifest hash.
    """

    version: int
    entries: tuple[PinEntry, ...]
    environments: tuple[PinEnvironment, ...]

    def to_canonical_bytes(self) -> bytes:
        """Serialise to canonical JSON bytes -- the hashed artifact."""
        return json.dumps(
            {
                "version": self.version,
                "environments": [e.to_dict() for e in self.environments],
                "entries": [e.to_dict() for e in self.entries],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def manifest_hash(self) -> str:
        """Return the manifest's content address (``sha256:<hex>``)."""
        return content_address(self.to_canonical_bytes())

    def entry(self, kind: str, name: str) -> PinEntry | None:
        """Return the pin for ``(kind, name)``, or ``None`` when unpinned."""
        for candidate in self.entries:
            if candidate.kind == kind and candidate.name == name:
                return candidate
        return None

    def environment(self, name: str) -> PinEnvironment | None:
        """Return the named environment, or ``None`` when it is not declared."""
        for candidate in self.environments:
            if candidate.name == name:
                return candidate
        return None

    def allowed_sources(self, name: str) -> frozenset[str]:
        """Return the sources ``name`` allows; empty when it is not declared."""
        env = self.environment(name)
        return frozenset(env.allowed_sources) if env is not None else frozenset()


@dataclass(frozen=True)
class LoadedComponent:
    """One plugin or skill the install actually resolved.

    The resolved set is supplied by the caller; recording it at spawn time
    is a separate concern and is not implemented here.
    """

    kind: str
    name: str
    version: str
    content_hash: str
    source: str


@dataclass(frozen=True)
class PinDrift:
    """One divergence between the loaded set and the manifest.

    Attributes:
        kind: Component kind, or ``""`` when the drift is not about one
            component (an undeclared environment, for instance).
        name: Component name, or ``""`` as above.
        reason: One of :data:`DRIFT_ENVIRONMENT`, :data:`DRIFT_UNPINNED`,
            :data:`DRIFT_ABSENT`, :data:`DRIFT_VERSION`,
            :data:`DRIFT_CONTENT_HASH`, :data:`DRIFT_SOURCE`.
        expected: What the manifest allows.
        actual: What was loaded.
    """

    kind: str
    name: str
    reason: str
    expected: str
    actual: str

    def describe(self) -> str:
        """Return a single operator-readable line naming this divergence."""
        subject = f"{self.kind}/{self.name}" if self.kind and self.name else "manifest"
        return f"{subject}: {self.reason} (expected {self.expected!r}, loaded {self.actual!r})"


@dataclass(frozen=True)
class PinVerifyResult:
    """The outcome of comparing a loaded set against the manifest."""

    drifts: tuple[PinDrift, ...]
    checked: int

    @property
    def ok(self) -> bool:
        """True when nothing diverged."""
        return not self.drifts

    @property
    def exit_code(self) -> int:
        """``0`` when clean, :data:`PIN_VERIFY_DRIFT_EXIT` otherwise."""
        return 0 if self.ok else PIN_VERIFY_DRIFT_EXIT

    def render_lines(self) -> list[str]:
        """Return one line per drifted entry, in a stable order."""
        return [d.describe() for d in self.drifts]


@dataclass(frozen=True)
class PinApplyRecord:
    """The decision record written by one :func:`apply_pin_manifest` call.

    Attributes:
        timestamp: Caller-supplied integer timestamp, so identical fixtures
            produce byte-identical ledger lines.
        manifest_hash_before: Content address of the previously applied
            manifest; ``""`` on the first apply.
        manifest_hash_after: Content address of the manifest now applied.
        changed: False when the apply was a no-op -- the recorded evidence
            that applying twice changed nothing.
        entry_count: Number of pinned components in the applied manifest.
    """

    timestamp: int
    manifest_hash_before: str
    manifest_hash_after: str
    changed: bool
    entry_count: int

    def to_canonical_bytes(self) -> bytes:
        """Serialise to one canonical JSON line (without the newline)."""
        return json.dumps(
            {
                "timestamp": self.timestamp,
                "manifest_hash_before": self.manifest_hash_before,
                "manifest_hash_after": self.manifest_hash_after,
                "changed": self.changed,
                "entry_count": self.entry_count,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> PinApplyRecord:
        """Rebuild a record from one parsed ledger line."""
        return cls(
            timestamp=int(row["timestamp"]),
            manifest_hash_before=str(row["manifest_hash_before"]),
            manifest_hash_after=str(row["manifest_hash_after"]),
            changed=bool(row["changed"]),
            entry_count=int(row["entry_count"]),
        )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def content_address(payload: bytes) -> str:
    """Return ``sha256:<hex>`` over *payload*."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _validate_pinned_version(version: str, label: str) -> list[str]:
    """Reject anything that is not an exact ``MAJOR.MINOR.PATCH`` version.

    Delegates the format check to
    :func:`~bernstein.core.plugins_core.plugin_manifest.is_exact_semver` --
    the same rule the per-plugin manifest applies -- and restates the
    failure at the install-manifest level, naming the entry so an operator
    can find it in a long manifest.
    """
    if not is_exact_semver(version):
        shown = version if version else "<empty>"
        return [
            f"{label}: version {shown!r} is not an exact pin. "
            "The manifest must name a MAJOR.MINOR.PATCH version (e.g. '1.0.0'); "
            "floating specifiers such as 'latest', '*', '^1.2.0' or '1.2' are rejected."
        ]
    return []


def _parse_environments(raw: Any) -> tuple[list[PinEnvironment], list[str]]:
    """Parse the ``environments:`` mapping into sorted environments."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return [], ["environments: must be a mapping of environment name to {allowed_sources: [...]}"]

    mapping = cast("dict[str, Any]", raw)
    if not mapping:
        return [], ["environments: at least one environment must be declared"]

    environments: list[PinEnvironment] = []
    for name in sorted(mapping):
        body: Any = mapping[name]
        if not isinstance(body, dict):
            errors.append(f"environments.{name}: must be a mapping with an 'allowed_sources' list")
            continue
        sources_raw: Any = cast("dict[str, Any]", body).get("allowed_sources", [])
        if not isinstance(sources_raw, list):
            errors.append(f"environments.{name}.allowed_sources: must be a list of source identifiers")
            continue
        sources = [str(s).strip() for s in cast("list[Any]", sources_raw) if str(s).strip()]
        if not sources:
            errors.append(f"environments.{name}.allowed_sources: must list at least one source")
            continue
        environments.append(PinEnvironment(name=name, allowed_sources=tuple(sorted(set(sources)))))

    return environments, errors


def _parse_entry(item: Any, kind: str, index: int, known_sources: frozenset[str]) -> tuple[PinEntry | None, list[str]]:
    """Parse and validate one ``plugins:``/``skills:`` entry."""
    label = f"{kind}s[{index}]"
    if not isinstance(item, dict):
        return None, [f"{label}: must be a mapping with name, version, content_hash and source"]

    row = cast("dict[str, Any]", item)
    name = str(row.get("name", "")).strip()
    label = f"{label} {name!r}" if name else label

    errors: list[str] = []
    if not name:
        errors.append(f"{label}: name is required")
    elif not _PIN_NAME_RE.match(name):
        errors.append(
            f"{label}: name contains invalid characters; only alphanumerics, '.', '-' and '_' are allowed",
        )

    version = str(row.get("version", "")).strip()
    errors.extend(_validate_pinned_version(version, label))

    content_hash = str(row.get("content_hash", "")).strip()
    if not _CONTENT_HASH_RE.match(content_hash):
        errors.append(
            f"{label}: content_hash {content_hash or '<empty>'!r} must be a full content address "
            "of the form 'sha256:<64 lowercase hex chars>'",
        )

    source = str(row.get("source", "")).strip()
    if not source:
        errors.append(f"{label}: source is required; a pin names where the component may come from")
    elif source not in known_sources:
        errors.append(
            f"{label}: source {source!r} is not listed in allowed_sources of any environment, "
            "so no environment could ever load it",
        )

    if errors:
        return None, errors
    return PinEntry(kind=kind, name=name, version=version, content_hash=content_hash, source=source), []


def parse_pin_manifest(data: Any) -> PinManifest:
    """Parse and validate an install-wide pin manifest mapping.

    Every entry must carry an exact version and a full content address, and
    must come from a source some environment allows. Validation is
    exhaustive: all failures are collected and reported together.

    Args:
        data: The parsed manifest document (a mapping).

    Returns:
        The validated :class:`PinManifest`.

    Raises:
        PinManifestError: When the document is not a mapping, declares an
            unsupported schema version, or holds any invalid entry --
            including a floating or ``latest`` version specifier.
    """
    if not isinstance(data, dict):
        raise PinManifestError(["pin manifest must be a mapping"])

    doc = cast("dict[str, Any]", data)
    errors: list[str] = []

    raw_version: Any = doc.get("version", PIN_MANIFEST_VERSION)
    try:
        schema_version = int(raw_version)
    except (TypeError, ValueError):
        schema_version = -1
    if schema_version != PIN_MANIFEST_VERSION:
        errors.append(
            f"version: unsupported pin manifest schema version {raw_version!r}; expected {PIN_MANIFEST_VERSION}"
        )
        schema_version = PIN_MANIFEST_VERSION

    environments, env_errors = _parse_environments(doc.get("environments"))
    errors.extend(env_errors)
    known_sources = frozenset(s for env in environments for s in env.allowed_sources)

    entries: list[PinEntry] = []
    seen: set[tuple[str, str]] = set()
    for kind in PIN_KINDS:
        raw_list: Any = doc.get(f"{kind}s", [])
        if raw_list is None:
            continue
        if not isinstance(raw_list, list):
            errors.append(f"{kind}s: must be a list of pinned entries")
            continue
        for index, item in enumerate(cast("list[Any]", raw_list)):
            entry, entry_errors = _parse_entry(item, kind, index, known_sources)
            errors.extend(entry_errors)
            if entry is None:
                continue
            key = (entry.kind, entry.name)
            if key in seen:
                errors.append(f"{kind}s[{index}] {entry.name!r}: duplicate pin for the same {kind}")
                continue
            seen.add(key)
            entries.append(entry)

    if errors:
        raise PinManifestError(errors)

    return PinManifest(
        version=schema_version,
        entries=tuple(sorted(entries, key=lambda e: (e.kind, e.name))),
        environments=tuple(sorted(environments, key=lambda e: e.name)),
    )


def load_pin_manifest(path: Path) -> PinManifest:
    """Load and validate the pin manifest at *path* (YAML or JSON).

    Raises:
        PinManifestError: When the file is missing, unreadable, unparseable,
            or fails validation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PinManifestError([f"cannot read pin manifest {path}: {exc}"]) from None

    raw: Any
    try:
        import yaml

        raw = yaml.safe_load(text)
    except ImportError:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PinManifestError([f"cannot parse pin manifest {path}: {exc}"]) from None
    except Exception as exc:  # pragma: no cover - yaml raises its own error tree
        raise PinManifestError([f"cannot parse pin manifest {path}: {exc}"]) from None

    return parse_pin_manifest(raw)


def loaded_components_from_json(raw: Any) -> list[LoadedComponent]:
    """Build a resolved set from a parsed JSON list of component records.

    Raises:
        PinManifestError: When the document is not a list of mappings, or a
            record is missing a field, or names an unknown kind.
    """
    if not isinstance(raw, list):
        raise PinManifestError(["loaded set must be a JSON list of component records"])

    errors: list[str] = []
    loaded: list[LoadedComponent] = []
    for index, item in enumerate(cast("list[Any]", raw)):
        if not isinstance(item, dict):
            errors.append(f"loaded[{index}]: must be a mapping")
            continue
        row = cast("dict[str, Any]", item)
        kind = str(row.get("kind", "")).strip()
        if kind not in PIN_KINDS:
            errors.append(f"loaded[{index}]: kind {kind!r} must be one of {list(PIN_KINDS)}")
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            errors.append(f"loaded[{index}]: name is required")
            continue
        loaded.append(
            LoadedComponent(
                kind=kind,
                name=name,
                version=str(row.get("version", "")).strip(),
                content_hash=str(row.get("content_hash", "")).strip(),
                source=str(row.get("source", "")).strip(),
            )
        )

    if errors:
        raise PinManifestError(errors)
    return loaded


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_pinned_set(
    manifest: PinManifest,
    loaded: Sequence[LoadedComponent],
    *,
    environment: str | None = None,
) -> PinVerifyResult:
    """Compare a resolved set against *manifest* and enumerate every drift.

    Presence is checked in both directions: a component that is loaded but
    unpinned is drift, and so is a pinned component the install did not
    load -- both mean the running install is not the one that was pinned.

    Source is checked independently of version and content hash, so a
    component pulled from a source *environment* does not allow is
    reported even when its bytes match the pin exactly.

    Args:
        manifest: The parsed allow-list.
        loaded: The components the install actually resolved.
        environment: Environment whose ``allowed_sources`` gate the loaded
            sources. When omitted, the source check is skipped.

    Returns:
        A :class:`PinVerifyResult`; ``exit_code`` is non-zero on any drift.
    """
    allowed: frozenset[str] | None = None
    if environment is not None:
        if manifest.environment(environment) is None:
            return PinVerifyResult(
                drifts=(
                    PinDrift(
                        kind="",
                        name="",
                        reason=DRIFT_ENVIRONMENT,
                        expected=", ".join(e.name for e in manifest.environments),
                        actual=environment,
                    ),
                ),
                checked=0,
            )
        allowed = manifest.allowed_sources(environment)

    drifts: list[PinDrift] = []
    seen: set[tuple[str, str]] = set()

    for component in sorted(loaded, key=lambda c: (c.kind, c.name)):
        seen.add((component.kind, component.name))
        pin = manifest.entry(component.kind, component.name)
        if pin is None:
            drifts.append(
                PinDrift(
                    kind=component.kind,
                    name=component.name,
                    reason=DRIFT_UNPINNED,
                    expected="",
                    actual=component.version,
                )
            )
            continue
        if component.version != pin.version:
            drifts.append(
                PinDrift(
                    kind=component.kind,
                    name=component.name,
                    reason=DRIFT_VERSION,
                    expected=pin.version,
                    actual=component.version,
                )
            )
        if component.content_hash != pin.content_hash:
            drifts.append(
                PinDrift(
                    kind=component.kind,
                    name=component.name,
                    reason=DRIFT_CONTENT_HASH,
                    expected=pin.content_hash,
                    actual=component.content_hash,
                )
            )
        if allowed is not None and component.source not in allowed:
            drifts.append(
                PinDrift(
                    kind=component.kind,
                    name=component.name,
                    reason=DRIFT_SOURCE,
                    expected=", ".join(sorted(allowed)),
                    actual=component.source,
                )
            )

    for pin in manifest.entries:
        if (pin.kind, pin.name) not in seen:
            drifts.append(
                PinDrift(
                    kind=pin.kind,
                    name=pin.name,
                    reason=DRIFT_ABSENT,
                    expected=pin.version,
                    actual="",
                )
            )

    return PinVerifyResult(drifts=tuple(drifts), checked=len(loaded))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def pin_state_dir(workdir: Path) -> Path:
    """Return the directory holding the applied state and the apply ledger."""
    return workdir.joinpath(*PIN_STATE_SUBPATH)


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write *payload* to *path* via a sibling temp file and one rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def apply_pin_manifest(manifest: PinManifest, *, workdir: Path, timestamp: int) -> PinApplyRecord:
    """Apply *manifest* to *workdir* and record the decision.

    The applied state is the manifest's canonical bytes. A second apply of
    the same manifest rewrites nothing, so the state file stays
    byte-identical, and the returned record carries ``changed=False`` with
    equal before and after hashes. Every apply -- no-ops included -- appends
    one line to the apply ledger, so the ledger is a complete history of
    what the install was told to allow.

    Args:
        manifest: The manifest to apply.
        workdir: Project root; state lands under ``.sdd/plugins/pins/``.
        timestamp: Integer timestamp recorded on the decision record.

    Returns:
        The :class:`PinApplyRecord` that was appended to the ledger.
    """
    applied_path = pin_state_dir(workdir) / PIN_APPLIED_FILENAME
    payload = manifest.to_canonical_bytes()

    before = ""
    if applied_path.is_file():
        before = content_address(applied_path.read_bytes())

    after = content_address(payload)
    changed = before != after
    if changed:
        _write_atomic(applied_path, payload)

    record = PinApplyRecord(
        timestamp=timestamp,
        manifest_hash_before=before,
        manifest_hash_after=after,
        changed=changed,
        entry_count=len(manifest.entries),
    )
    _append_apply_record(workdir, record)
    return record


def _append_apply_record(workdir: Path, record: PinApplyRecord) -> None:
    """Append one canonical JSON line to the apply ledger."""
    path = pin_state_dir(workdir) / PIN_DECISIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(record.to_canonical_bytes() + b"\n")


def read_apply_records(workdir: Path) -> list[PinApplyRecord]:
    """Return every apply decision record for *workdir*, oldest first.

    Malformed lines are skipped and logged; a truncated tail must not hide
    the applies that were recorded correctly.
    """
    path = pin_state_dir(workdir) / PIN_DECISIONS_FILENAME
    if not path.is_file():
        return []

    records: list[PinApplyRecord] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            row: Any = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("record must be a mapping")
            records.append(PinApplyRecord.from_dict(cast("dict[str, Any]", row)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.warning("plugin_pin_manifest: malformed apply record at %s:%d: %s", path, lineno, exc)
    return records


__all__ = [
    "DRIFT_ABSENT",
    "DRIFT_CONTENT_HASH",
    "DRIFT_ENVIRONMENT",
    "DRIFT_SOURCE",
    "DRIFT_UNPINNED",
    "DRIFT_VERSION",
    "PIN_APPLIED_FILENAME",
    "PIN_DECISIONS_FILENAME",
    "PIN_KINDS",
    "PIN_KIND_PLUGIN",
    "PIN_KIND_SKILL",
    "PIN_MANIFEST_VERSION",
    "PIN_STATE_SUBPATH",
    "PIN_VERIFY_DRIFT_EXIT",
    "LoadedComponent",
    "PinApplyRecord",
    "PinDrift",
    "PinEntry",
    "PinEnvironment",
    "PinManifest",
    "PinManifestError",
    "PinVerifyResult",
    "apply_pin_manifest",
    "content_address",
    "load_pin_manifest",
    "loaded_components_from_json",
    "parse_pin_manifest",
    "pin_state_dir",
    "read_apply_records",
    "verify_pinned_set",
]
