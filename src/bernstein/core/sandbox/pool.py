"""Named sandbox pools: chain-projected manifests and governed overrides (#2547).

Placement of agent work used to be implicit and per-run: the selector
(:mod:`bernstein.core.sandbox.selector`) picked a backend from a flat
:class:`~bernstein.core.sandbox.selector.SandboxPolicy` plus a per-run
:class:`~bernstein.core.sandbox.manifest.WorkspaceManifest`, and nothing
persisted between runs. Whoever authored a recipe controlled every infra
knob the manifest exposed. With agent-authored recipes in the loop an agent
could request more network or credentials than the operator intended.

A :class:`PoolManifest` makes the pool a first-class, frozen, canonicalized
document whose identity *is* its canonical hash (:attr:`PoolManifest.pool_hash`),
following the ``canonical_json`` plus sha256 pattern in
:mod:`bernstein.core.config.manifest`. The pool declares:

* the backend allowlist placement may choose from,
* a base workspace template (root / env / timeout),
* the exact set of override fields it exposes to recipes,
* pool-level concurrency, and
* a capability ceiling: sandbox capabilities, a network-egress class, and a
  credential env-var allowlist bound to ``credential_scoping`` known keys.

A recipe targets a pool by name and may set *only* the fields the pool
exposes. :func:`merge_pool_overrides` is a pure function: it schema-validates
the overrides, merges them into the base template, and canonicalizes the
result into an :attr:`PoolMergeResult.effective_manifest_hash`. Two hosts
holding the same pool manifest and the same recipe compute a byte-identical
effective manifest (AC: determinism).

The merge is fail-closed. An override that touches a non-exposed field,
requests a capability above the ceiling, widens the network egress class, or
names a credential env var outside the pool allowlist raises
:class:`PoolOverrideRefused` *before anything starts* -- the caller seals the
refusal as its own chained receipt and never creates a sandbox (AC: isolation,
fail closed). An agent-authored recipe can never grant itself more than the
pool template exposes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.manifest import WorkspaceManifest

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Wire-format version stamped into every pool manifest. Bump only on a
#: breaking change to the canonical payload shape.
POOL_MANIFEST_SCHEMA_VERSION = 1

#: Minimal capabilities every effective placement requires; mirrors the
#: selector's :class:`SandboxPolicy` default so a pool never selects a
#: backend that cannot read/write files or execute.
BASE_CAPABILITIES: frozenset[SandboxCapability] = frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})

#: Network egress classes ordered from most to least restrictive. An
#: override may only request an egress class at or below the pool ceiling;
#: a request for a wider class is refused.
NETWORK_EGRESS_CLASSES: tuple[str, ...] = ("none", "loopback", "restricted", "open")
_EGRESS_RANK: dict[str, int] = {name: index for index, name in enumerate(NETWORK_EGRESS_CLASSES)}

#: The complete set of override keys a pool may choose to expose. Every key
#: a recipe sets must be in the pool's ``exposed_fields`` *and* in this set;
#: an unknown key is always refused.
KNOWN_OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {"root", "env", "timeout_seconds", "backend", "capabilities", "network_egress_class"}
)


class PoolManifestError(ValueError):
    """Raised when a :class:`PoolManifest` is constructed with invalid fields."""


class PoolOverrideRefused(Exception):
    """A governed override was refused by the pool ceiling (fail closed).

    Carries a structured ``reason`` and the offending ``field`` so callers can
    seal a chained refusal receipt without re-parsing the message. No sandbox
    is created when this is raised.

    Reason codes:
        ``non_exposed_field``     -- the override key is not exposed by the pool.
        ``unknown_field``         -- the override key is not a known field at all.
        ``backend_not_allowed``   -- the requested backend is outside the allowlist.
        ``capability_above_ceiling`` -- a requested capability exceeds the ceiling.
        ``egress_widened``        -- the requested egress class is wider than the ceiling.
        ``credential_env_not_allowed`` -- a credential env var is outside the allowlist.
        ``malformed_value``       -- the override value has the wrong type/shape.
    """

    def __init__(self, reason: str, *, field: str, detail: str = "") -> None:
        message = f"pool override refused: {reason} (field={field!r})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.reason = reason
        self.field = field
        self.detail = detail


def _canonical_json(payload: Any) -> str:
    """Deterministic JSON string: sorted keys, compact separators, UTF-8 safe."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    """SHA-256 hex digest over the UTF-8 bytes of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sorted_cap_values(caps: Iterable[SandboxCapability]) -> list[str]:
    """Return capability string values in a stable, canonical order."""
    return sorted(c.value for c in caps)


@dataclass(frozen=True)
class PoolWorkspaceTemplate:
    """JSON-safe base workspace template a pool exposes to recipes.

    A deliberately small, canonicalizable subset of
    :class:`~bernstein.core.sandbox.manifest.WorkspaceManifest`: the fields
    an operator wants recipe authors to be able to tune. Byte-injected files
    and cloud mounts are intentionally *not* part of the pool template -- those
    are orchestrator-side concerns, not recipe knobs.

    Attributes:
        root: Absolute workspace root inside the sandbox.
        env: Environment variables seeded for every exec. Stored as a plain
            mapping; canonicalized with sorted keys so order never affects the
            hash.
        timeout_seconds: Default wall-clock exec timeout.
    """

    root: str = "/workspace"
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    timeout_seconds: int = 1800

    def to_canonical(self) -> dict[str, Any]:
        """Return the canonical, JSON-safe payload for hashing."""
        return {
            "root": self.root,
            "env": {str(k): str(v) for k, v in sorted(self.env.items())},
            "timeout_seconds": int(self.timeout_seconds),
        }

    def template_hash(self) -> str:
        """SHA-256 over the canonical template payload."""
        return _sha256_hex(_canonical_json(self.to_canonical()))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PoolWorkspaceTemplate:
        raw_env = data.get("env", {}) or {}
        return cls(
            root=str(data.get("root", "/workspace")),
            env={str(k): str(v) for k, v in dict(raw_env).items()},
            timeout_seconds=int(data.get("timeout_seconds", 1800)),
        )


@dataclass(frozen=True)
class PoolManifest:
    """A frozen, canonicalized named sandbox pool (#2547).

    The manifest's identity is its :attr:`pool_hash`: a SHA-256 digest over the
    canonical JSON of every other field. Pools are never mutated in place --
    they are registered, updated, and retired only by appending ``pool.*``
    events to the HMAC audit chain, and the runtime registry is a deterministic
    projection rebuilt by replaying those events (see
    :mod:`bernstein.core.sandbox.pool_registry`).

    Attributes:
        name: Operator-facing pool name a recipe targets.
        backend_allowlist: Backend names placement may choose from, in
            operator-preferred order. Empty means "any registered backend".
        template: Base :class:`PoolWorkspaceTemplate`.
        exposed_fields: The exact override keys recipes may set. Every entry
            must be in :data:`KNOWN_OVERRIDE_FIELDS`.
        max_concurrency: Pool-level concurrency ceiling (0 == unbounded).
        capability_ceiling: Maximum sandbox capabilities an effective
            placement may require. Must include :data:`BASE_CAPABILITIES`.
        network_egress_class: Widest egress class placement may request.
        credential_env_allowlist: Credential env-var names a recipe override
            may introduce, bound to ``credential_scoping`` known keys.
        schema_version: Wire-format version.
        pool_hash: SHA-256 over the canonical payload; the pool identity.
    """

    name: str
    backend_allowlist: tuple[str, ...] = ()
    template: PoolWorkspaceTemplate = field(default_factory=PoolWorkspaceTemplate)
    exposed_fields: tuple[str, ...] = ()
    max_concurrency: int = 0
    capability_ceiling: frozenset[SandboxCapability] = field(default_factory=lambda: frozenset(BASE_CAPABILITIES))
    network_egress_class: str = "none"
    credential_env_allowlist: frozenset[str] = field(default_factory=frozenset)
    schema_version: int = POOL_MANIFEST_SCHEMA_VERSION
    pool_hash: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise PoolManifestError("pool name must be non-empty")
        unknown = set(self.exposed_fields) - KNOWN_OVERRIDE_FIELDS
        if unknown:
            raise PoolManifestError(f"exposed_fields contains unknown keys: {sorted(unknown)!r}")
        if self.network_egress_class not in _EGRESS_RANK:
            raise PoolManifestError(
                f"network_egress_class {self.network_egress_class!r} not one of {NETWORK_EGRESS_CLASSES!r}"
            )
        missing_base = BASE_CAPABILITIES - self.capability_ceiling
        if missing_base:
            missing = ", ".join(sorted(c.value for c in missing_base))
            raise PoolManifestError(f"capability_ceiling must include base capabilities: {missing}")
        computed = self.compute_hash()
        if self.pool_hash and self.pool_hash != computed:
            raise PoolManifestError("pool_hash does not match the canonical payload")
        if not self.pool_hash:
            object.__setattr__(self, "pool_hash", computed)

    # -- canonical JSON & hashing ------------------------------------------

    def _canonical_payload(self) -> dict[str, Any]:
        """Return the JSON-safe payload used for hashing (excludes pool_hash)."""
        return {
            "name": self.name,
            "backend_allowlist": list(self.backend_allowlist),
            "template": self.template.to_canonical(),
            "exposed_fields": sorted(self.exposed_fields),
            "max_concurrency": int(self.max_concurrency),
            "capability_ceiling": _sorted_cap_values(self.capability_ceiling),
            "network_egress_class": self.network_egress_class,
            "credential_env_allowlist": sorted(self.credential_env_allowlist),
            "schema_version": int(self.schema_version),
        }

    def canonical_json(self) -> str:
        """Deterministic JSON string over the canonical payload."""
        return _canonical_json(self._canonical_payload())

    def compute_hash(self) -> str:
        """SHA-256 over the canonical JSON payload."""
        return _sha256_hex(self.canonical_json())

    def to_dict(self) -> dict[str, Any]:
        """Full dict including ``pool_hash`` (for on-disk / event storage)."""
        payload = self._canonical_payload()
        payload["pool_hash"] = self.pool_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PoolManifest:
        """Reconstruct a manifest from a stored dict, verifying the hash."""
        caps = frozenset(SandboxCapability(v) for v in data.get("capability_ceiling", ()))
        if not caps:
            caps = frozenset(BASE_CAPABILITIES)
        return cls(
            name=str(data["name"]),
            backend_allowlist=tuple(data.get("backend_allowlist", ()) or ()),
            template=PoolWorkspaceTemplate.from_dict(data.get("template", {}) or {}),
            exposed_fields=tuple(data.get("exposed_fields", ()) or ()),
            max_concurrency=int(data.get("max_concurrency", 0)),
            capability_ceiling=caps,
            network_egress_class=str(data.get("network_egress_class", "none")),
            credential_env_allowlist=frozenset(data.get("credential_env_allowlist", ()) or ()),
            schema_version=int(data.get("schema_version", POOL_MANIFEST_SCHEMA_VERSION)),
            pool_hash=str(data.get("pool_hash", "")),
        )


@dataclass(frozen=True)
class PoolMergeResult:
    """Deterministic result of merging a recipe's overrides into a pool.

    Every hash here is a bare 64-char SHA-256 hex digest. Two hosts holding the
    same :class:`PoolManifest` and the same overrides produce byte-identical
    values for all fields (AC: determinism).

    Attributes:
        pool_hash: Identity of the pool the merge targeted.
        template_hash: Hash of the pool base template.
        overrides_hash: Hash of the canonical overrides dict.
        effective_manifest_hash: Hash pinning the full effective placement.
        effective_template: The merged :class:`PoolWorkspaceTemplate`.
        backend_override: Backend forced by an exposed ``backend`` override, or
            ``None`` for no override (selection stays cost-first over the
            allowlist).
        capabilities: Effective required capabilities (base plus any exposed
            capability overrides, always within the ceiling).
        network_egress_class: Effective egress class (at or below the ceiling).
        credential_env: Sorted credential env-var names the effective manifest
            introduced, all within the pool allowlist.
    """

    pool_hash: str
    template_hash: str
    overrides_hash: str
    effective_manifest_hash: str
    effective_template: PoolWorkspaceTemplate
    backend_override: str | None
    capabilities: frozenset[SandboxCapability]
    network_egress_class: str
    credential_env: tuple[str, ...]

    def to_workspace_manifest(self) -> WorkspaceManifest:
        """Materialise a per-run :class:`WorkspaceManifest` from the merge."""
        return WorkspaceManifest(
            root=self.effective_template.root,
            env=dict(self.effective_template.env),
            timeout_seconds=self.effective_template.timeout_seconds,
        )

    def effective_payload(self) -> dict[str, Any]:
        """Canonical JSON-safe payload the ``effective_manifest_hash`` covers."""
        return {
            "pool_hash": self.pool_hash,
            "template": self.effective_template.to_canonical(),
            "backend_override": self.backend_override,
            "capabilities": _sorted_cap_values(self.capabilities),
            "network_egress_class": self.network_egress_class,
            "credential_env": list(self.credential_env),
        }


def canonical_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical, JSON-safe copy of *overrides* for hashing.

    Nested ``env`` maps are key-sorted and ``capabilities`` lists are
    value-sorted so a recipe that specifies the same overrides in a different
    textual order hashes identically (AC: determinism across permutations).
    """
    out: dict[str, Any] = {}
    for key in sorted(overrides):
        value = overrides[key]
        if key == "env" and isinstance(value, Mapping):
            out[key] = {str(k): str(v) for k, v in sorted(value.items())}
        elif key == "capabilities" and isinstance(value, (list, tuple, set, frozenset)):
            out[key] = sorted(str(v) for v in value)
        else:
            out[key] = value
    return out


def overrides_hash(overrides: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical overrides payload."""
    return _sha256_hex(_canonical_json(canonical_overrides(overrides)))


def merge_pool_overrides(
    pool: PoolManifest,
    overrides: Mapping[str, Any] | None = None,
    *,
    known_credential_keys: Iterable[str] | None = None,
) -> PoolMergeResult:
    """Merge recipe *overrides* into *pool*, fail-closed against its ceiling.

    A pure function: no I/O, no registry side effects. The returned
    :class:`PoolMergeResult` is byte-stable for a given ``(pool, overrides)``
    pair regardless of override key order (AC: determinism).

    Args:
        pool: The target pool manifest.
        overrides: Recipe-supplied override map. Every key must be in
            ``pool.exposed_fields``.
        known_credential_keys: The credential namespace (``credential_scoping``
            known keys). An env var in this set may only be introduced by an
            override when it is also in ``pool.credential_env_allowlist``. When
            omitted, the process default credential policy's known keys are
            used; an empty namespace makes the credential gate inert (matching
            ``credential_scoping``'s own inert-when-empty behaviour).

    Returns:
        The deterministic :class:`PoolMergeResult`.

    Raises:
        PoolOverrideRefused: The override touches a non-exposed field, requests
            a capability above the ceiling, widens egress, or names a
            credential env var outside the allowlist. No sandbox is created.
    """
    overrides = dict(overrides or {})
    exposed = set(pool.exposed_fields)

    for key in overrides:
        if key not in KNOWN_OVERRIDE_FIELDS:
            raise PoolOverrideRefused("unknown_field", field=key)
        if key not in exposed:
            raise PoolOverrideRefused("non_exposed_field", field=key)

    cred_namespace = _resolve_known_credential_keys(known_credential_keys)

    effective_template = _merge_template(pool, overrides, cred_namespace)
    backend_override = _resolve_backend_override(pool, overrides)
    capabilities = _resolve_capabilities(pool, overrides)
    egress = _resolve_egress(pool, overrides)
    credential_env = _effective_credential_env(effective_template, cred_namespace)

    result = PoolMergeResult(
        pool_hash=pool.pool_hash,
        template_hash=pool.template.template_hash(),
        overrides_hash=overrides_hash(overrides),
        effective_manifest_hash="",
        effective_template=effective_template,
        backend_override=backend_override,
        capabilities=capabilities,
        network_egress_class=egress,
        credential_env=credential_env,
    )
    effective_hash = _sha256_hex(_canonical_json(result.effective_payload()))
    object.__setattr__(result, "effective_manifest_hash", effective_hash)
    return result


def _resolve_known_credential_keys(known: Iterable[str] | None) -> frozenset[str]:
    """Resolve the credential namespace, defaulting to the process policy."""
    if known is not None:
        return frozenset(known)
    try:
        from bernstein.core.credential_scoping import get_default_policy

        return frozenset(get_default_policy().known_keys)
    except Exception:
        return frozenset()


def _merge_template(
    pool: PoolManifest,
    overrides: Mapping[str, Any],
    cred_namespace: frozenset[str],
) -> PoolWorkspaceTemplate:
    """Merge the exposed template fields; refuse credential env additions."""
    base = pool.template
    root = base.root
    timeout = base.timeout_seconds
    env = dict(base.env)

    if "root" in overrides:
        if not isinstance(overrides["root"], str):
            raise PoolOverrideRefused("malformed_value", field="root", detail="root must be a string")
        root = overrides["root"]
    if "timeout_seconds" in overrides:
        try:
            timeout = int(overrides["timeout_seconds"])
        except (TypeError, ValueError) as exc:
            raise PoolOverrideRefused("malformed_value", field="timeout_seconds", detail=str(exc)) from exc
    if "env" in overrides:
        raw = overrides["env"]
        if not isinstance(raw, Mapping):
            raise PoolOverrideRefused("malformed_value", field="env", detail="env must be a mapping")
        for name, value in raw.items():
            name = str(name)
            # A credential-namespace env var may only be introduced by an
            # override when the pool explicitly allowlists it (fail closed).
            if name in cred_namespace and name not in pool.credential_env_allowlist:
                raise PoolOverrideRefused(
                    "credential_env_not_allowed",
                    field="env",
                    detail=f"credential env var {name!r} is outside the pool allowlist",
                )
            env[name] = str(value)

    return PoolWorkspaceTemplate(root=root, env=env, timeout_seconds=timeout)


def _resolve_backend_override(pool: PoolManifest, overrides: Mapping[str, Any]) -> str | None:
    """Resolve an exposed ``backend`` override against the allowlist."""
    if "backend" not in overrides:
        return None
    backend = overrides["backend"]
    if not isinstance(backend, str) or not backend:
        raise PoolOverrideRefused("malformed_value", field="backend", detail="backend must be a non-empty string")
    if pool.backend_allowlist and backend not in pool.backend_allowlist:
        raise PoolOverrideRefused(
            "backend_not_allowed",
            field="backend",
            detail=f"{backend!r} not in allowlist {list(pool.backend_allowlist)!r}",
        )
    return backend


def _resolve_capabilities(pool: PoolManifest, overrides: Mapping[str, Any]) -> frozenset[SandboxCapability]:
    """Resolve requested capabilities, bounded by the pool ceiling."""
    requested: set[SandboxCapability] = set(BASE_CAPABILITIES)
    if "capabilities" in overrides:
        raw = overrides["capabilities"]
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise PoolOverrideRefused("malformed_value", field="capabilities", detail="capabilities must be a list")
        for entry in raw:
            try:
                cap = SandboxCapability(str(entry))
            except ValueError as exc:
                raise PoolOverrideRefused(
                    "malformed_value", field="capabilities", detail=f"unknown capability {entry!r}"
                ) from exc
            if cap not in pool.capability_ceiling:
                raise PoolOverrideRefused(
                    "capability_above_ceiling",
                    field="capabilities",
                    detail=f"capability {cap.value!r} exceeds the pool ceiling",
                )
            requested.add(cap)
    return frozenset(requested)


def _resolve_egress(pool: PoolManifest, overrides: Mapping[str, Any]) -> str:
    """Resolve the requested egress class, refusing any widening."""
    if "network_egress_class" not in overrides:
        return pool.network_egress_class
    requested = overrides["network_egress_class"]
    if requested not in _EGRESS_RANK:
        raise PoolOverrideRefused(
            "malformed_value",
            field="network_egress_class",
            detail=f"{requested!r} not one of {NETWORK_EGRESS_CLASSES!r}",
        )
    if _EGRESS_RANK[requested] > _EGRESS_RANK[pool.network_egress_class]:
        raise PoolOverrideRefused(
            "egress_widened",
            field="network_egress_class",
            detail=f"requested {requested!r} is wider than the ceiling {pool.network_egress_class!r}",
        )
    return requested


def _effective_credential_env(template: PoolWorkspaceTemplate, cred_namespace: frozenset[str]) -> tuple[str, ...]:
    """Return the sorted credential env-var names the effective template sets."""
    return tuple(sorted(name for name in template.env if name in cred_namespace))


__all__ = [
    "BASE_CAPABILITIES",
    "KNOWN_OVERRIDE_FIELDS",
    "NETWORK_EGRESS_CLASSES",
    "POOL_MANIFEST_SCHEMA_VERSION",
    "PoolManifest",
    "PoolManifestError",
    "PoolMergeResult",
    "PoolOverrideRefused",
    "PoolWorkspaceTemplate",
    "canonical_overrides",
    "merge_pool_overrides",
    "overrides_hash",
]
