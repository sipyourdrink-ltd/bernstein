"""Named team manifests (issue #2248).

A team manifest is a single named, content-hashed unit describing a
working team: the role list, a model policy per role, a response profile
per role, coordination settings, and pinned digests of the role
templates the team spawns from. Manifests live at
``templates/teams/<name>.toml`` and are referenced from ``bernstein.yaml``
via ``team_manifest: <name>[@sha256]``.

Canonical serialization
-----------------------

The manifest digest is the SHA-256 of a *canonical* TOML serialization,
defined precisely so two operators can prove their team configurations
match byte-for-byte:

* UTF-8, LF (``\\n``) line endings, one trailing newline.
* Keys within every table are emitted in sorted (codepoint) order.
* Strings are TOML basic strings quoted via the skills lockfile quoting
  rules; integers are bare; booleans are ``true``/``false``.
* ``[[roles]]`` tables appear in declaration order (role order is part
  of the team's content); each role's ``model_policy`` sub-table is
  emitted only when non-empty.
* The ``[coordination]`` table is always emitted with defaults resolved,
  so an omitted table and an explicitly-default table hash identically.
* ``[role_template_digests]`` keys are quoted and sorted.
* The optional ``signature`` / ``signer_pubkey`` keys are excluded from
  the canonical form (they attest the content and cannot be part of it).

The canonical form is itself valid TOML: serializing a loaded manifest
and reloading it is a fixpoint, which the tests pin.

Signing
-------

Third-party manifests reuse the Ed25519 detached-signature path of the
skills catalog (:mod:`bernstein.core.skills.catalog.signature`): the
signature covers the canonical serialization bytes. A manifest that
carries a ``signature`` key is verified at load time and refuses to load
on mismatch.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]
from bernstein.core.config.seed_config import SeedError
from bernstein.core.skills.catalog.signature import (
    ManifestSignatureError,
    VerificationOutcome,
    sign_payload,
    verify_payload,
)
from bernstein.core.skills.lifecycle import _toml_quote

if TYPE_CHECKING:
    from pathlib import Path

#: Directory name for team manifests under ``templates/``.
TEAM_MANIFEST_DIR_NAME = "teams"

#: ``<name>`` or ``<name>@<64-hex-sha256>``.
_MANIFEST_REF_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:@(?P<digest>[0-9a-fA-F]{64}))?$")

_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: Keys attesting the manifest; excluded from the canonical serialization.
_SIGNATURE_KEYS = frozenset({"signature", "signer_pubkey"})

_TOP_LEVEL_KEYS = frozenset({"name", "version", "roles", "coordination", "role_template_digests"}) | _SIGNATURE_KEYS
_ROLE_KEYS = frozenset({"role", "model_policy", "response_profile"})
_COORDINATION_KEYS = frozenset({"review_chain", "parallelism"})


class TeamManifestError(SeedError):
    """Base error for team manifest handling.

    Subclasses :class:`bernstein.core.config.seed_config.SeedError` so a
    bad manifest reference refuses a run through the existing seed-error
    surface while staying individually catchable.
    """


class TeamManifestNotFoundError(TeamManifestError):
    """The referenced manifest does not exist in any search directory."""


class TeamManifestValidationError(TeamManifestError):
    """The manifest file is malformed or violates the schema."""


class TeamManifestDigestMismatchError(TeamManifestError):
    """A ``name@sha256`` pin does not match the resolved manifest digest."""


class TeamManifestSignatureError(TeamManifestError):
    """A signed manifest fails Ed25519 verification."""


@dataclass(frozen=True)
class TeamRoleSpec:
    """One ``[[roles]]`` entry of a team manifest.

    Attributes:
        role: Role name; must match a role template directory.
        model_policy: Per-role model policy keys (``model``, ``provider``,
            ``effort``, ``max_tokens``, ...). Values are strings or ints;
            validation against the closed key vocabulary happens in the
            seed parser, which the expansion feeds into.
        response_profile: Optional response-style profile name; expands to
            the ``response_style`` role policy key (issue #2243).
    """

    role: str
    model_policy: dict[str, str | int] = field(default_factory=dict)
    response_profile: str | None = None


@dataclass(frozen=True)
class TeamCoordination:
    """The ``[coordination]`` table of a team manifest.

    Attributes:
        review_chain: Whether the team runs a review chain.
        parallelism: Maximum concurrent workers the team is sized for.
    """

    review_chain: bool = False
    parallelism: int = 1


@dataclass(frozen=True)
class TeamManifest:
    """A parsed, validated team manifest.

    Attributes:
        name: Manifest name (also the reference name in ``bernstein.yaml``).
        version: Semantic version string of the manifest.
        roles: Role entries in declaration order.
        coordination: Coordination settings with defaults resolved.
        role_template_digests: Pinned SHA-256 per role template directory;
            keys are a subset of the declared role names.
        source_path: File the manifest was loaded from; ``None`` for
            manifests built in memory. Not part of the canonical form.
    """

    name: str
    version: str
    roles: tuple[TeamRoleSpec, ...]
    coordination: TeamCoordination = field(default_factory=TeamCoordination)
    role_template_digests: dict[str, str] = field(default_factory=dict)
    source_path: Path | None = None

    def digest(self) -> str:
        """Return the SHA-256 hex digest of the canonical serialization."""
        return hashlib.sha256(canonical_toml(self).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExpandedTeam:
    """Result of :func:`expand_manifest` - plain seed-config structures.

    Attributes:
        team: Role names in manifest order, the shape of the seed ``team:``
            list.
        role_model_policy: Raw per-role policy mapping, the shape the seed
            parser's ``role_model_policy`` validator accepts. Roles with an
            empty policy are omitted.
    """

    team: list[str]
    role_model_policy: dict[str, dict[str, str | int]]


def _scalar_toml(value: str | int | bool) -> str:
    """Render a scalar as canonical TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _toml_quote(value)


def canonical_toml(manifest: TeamManifest) -> str:
    """Return the canonical TOML serialization of *manifest*.

    See the module docstring for the exact rules. The output is valid
    TOML and reloading it yields a manifest with the same digest.
    """
    lines: list[str] = [
        f"name = {_toml_quote(manifest.name)}",
        f"version = {_toml_quote(manifest.version)}",
        "",
        "[coordination]",
        f"parallelism = {manifest.coordination.parallelism}",
        f"review_chain = {_scalar_toml(manifest.coordination.review_chain)}",
    ]
    for role in manifest.roles:
        lines.extend(("", "[[roles]]"))
        role_scalars: dict[str, str | int | bool] = {"role": role.role}
        if role.response_profile is not None:
            role_scalars["response_profile"] = role.response_profile
        for key in sorted(role_scalars):
            lines.append(f"{key} = {_scalar_toml(role_scalars[key])}")
        if role.model_policy:
            lines.extend(("", "[roles.model_policy]"))
            for key in sorted(role.model_policy):
                lines.append(f"{key} = {_scalar_toml(role.model_policy[key])}")
    if manifest.role_template_digests:
        lines.extend(("", "[role_template_digests]"))
        for role_name in sorted(manifest.role_template_digests):
            lines.append(f"{_toml_quote(role_name)} = {_toml_quote(manifest.role_template_digests[role_name])}")
    return "\n".join(lines) + "\n"


def _require_str(raw: dict[str, object], key: str, *, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise TeamManifestValidationError(f"{context}: {key!r} must be a non-empty string")
    return value


def _parse_role_entry(index: int, raw: object, *, context: str) -> TeamRoleSpec:
    if not isinstance(raw, dict):
        raise TeamManifestValidationError(f"{context}: roles[{index}] must be a table")
    entry = cast("dict[str, object]", raw)
    unknown = sorted(set(entry) - _ROLE_KEYS)
    if unknown:
        raise TeamManifestValidationError(f"{context}: roles[{index}] has unknown keys: {', '.join(unknown)}")
    role = _require_str(entry, "role", context=f"{context}: roles[{index}]")

    model_policy: dict[str, str | int] = {}
    raw_policy = entry.get("model_policy")
    if raw_policy is not None:
        if not isinstance(raw_policy, dict):
            raise TeamManifestValidationError(f"{context}: roles[{index}].model_policy must be a table")
        for key, value in cast("dict[str, object]", raw_policy).items():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise TeamManifestValidationError(
                    f"{context}: roles[{index}].model_policy[{key!r}] must be a string or integer"
                )
            model_policy[key] = value

    response_profile: str | None = None
    raw_profile = entry.get("response_profile")
    if raw_profile is not None:
        if not isinstance(raw_profile, str) or not raw_profile:
            raise TeamManifestValidationError(
                f"{context}: roles[{index}].response_profile must be a non-empty string"
            )
        response_profile = raw_profile

    return TeamRoleSpec(role=role, model_policy=model_policy, response_profile=response_profile)


def _parse_coordination(raw: object, *, context: str) -> TeamCoordination:
    if raw is None:
        return TeamCoordination()
    if not isinstance(raw, dict):
        raise TeamManifestValidationError(f"{context}: coordination must be a table")
    table = cast("dict[str, object]", raw)
    unknown = sorted(set(table) - _COORDINATION_KEYS)
    if unknown:
        raise TeamManifestValidationError(f"{context}: coordination has unknown keys: {', '.join(unknown)}")

    review_chain = table.get("review_chain", False)
    if not isinstance(review_chain, bool):
        raise TeamManifestValidationError(f"{context}: coordination.review_chain must be a boolean")

    parallelism = table.get("parallelism", 1)
    if isinstance(parallelism, bool) or not isinstance(parallelism, int) or parallelism < 1:
        raise TeamManifestValidationError(f"{context}: coordination.parallelism must be a positive integer")

    return TeamCoordination(review_chain=review_chain, parallelism=parallelism)


def _parse_role_template_digests(raw: object, role_names: set[str], *, context: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TeamManifestValidationError(f"{context}: role_template_digests must be a table")
    digests: dict[str, str] = {}
    for role_name, value in cast("dict[str, object]", raw).items():
        if role_name not in role_names:
            raise TeamManifestValidationError(
                f"{context}: role_template_digests pins undeclared role {role_name!r}"
            )
        if not isinstance(value, str) or not _HEX_DIGEST_RE.match(value):
            raise TeamManifestValidationError(
                f"{context}: role_template_digests[{role_name!r}] must be a 64-char lowercase hex sha256"
            )
        digests[role_name] = value
    return digests


def _verify_load_signature(data: dict[str, object], manifest: TeamManifest, *, context: str) -> None:
    """Verify an embedded ``signature`` / ``signer_pubkey`` pair, if any."""
    signature = data.get("signature")
    signer_pubkey = data.get("signer_pubkey")
    if signature is None and signer_pubkey is None:
        return
    if signature is not None and not isinstance(signature, str):
        raise TeamManifestValidationError(f"{context}: signature must be a string")
    if signer_pubkey is not None and not isinstance(signer_pubkey, str):
        raise TeamManifestValidationError(f"{context}: signer_pubkey must be a string")
    try:
        verify_team_manifest(manifest, cast("str | None", signature), cast("str | None", signer_pubkey))
    except ManifestSignatureError as exc:
        raise TeamManifestSignatureError(f"{context}: {exc}") from exc


def load_team_manifest(path: Path) -> TeamManifest:
    """Load and validate a team manifest from *path*.

    Args:
        path: Path to a ``<name>.toml`` manifest file.

    Returns:
        The parsed :class:`TeamManifest` with ``source_path`` set.

    Raises:
        TeamManifestNotFoundError: If *path* does not exist.
        TeamManifestValidationError: On schema violations or bad TOML.
        TeamManifestSignatureError: If an embedded signature fails to
            verify against the embedded ``signer_pubkey``.
    """
    context = str(path)
    if not path.is_file():
        raise TeamManifestNotFoundError(f"team manifest not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TeamManifestValidationError(f"{context}: cannot read manifest: {exc}") from exc
    try:
        data_raw: object = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TeamManifestValidationError(f"{context}: invalid TOML: {exc}") from exc
    if not isinstance(data_raw, dict):  # pragma: no cover - tomllib always returns a dict
        raise TeamManifestValidationError(f"{context}: manifest must be a TOML table")
    data = cast("dict[str, object]", data_raw)

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise TeamManifestValidationError(f"{context}: unknown keys: {', '.join(unknown)}")

    name = _require_str(data, "name", context=context)
    version = _require_str(data, "version", context=context)

    raw_roles = data.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise TeamManifestValidationError(f"{context}: roles must be a non-empty array of tables")
    roles = tuple(
        _parse_role_entry(i, raw_role, context=context)
        for i, raw_role in enumerate(cast("list[object]", raw_roles))
    )
    role_names = [r.role for r in roles]
    if len(role_names) != len(set(role_names)):
        raise TeamManifestValidationError(f"{context}: duplicate role names in roles array")

    coordination = _parse_coordination(data.get("coordination"), context=context)
    digests = _parse_role_template_digests(data.get("role_template_digests"), set(role_names), context=context)

    manifest = TeamManifest(
        name=name,
        version=version,
        roles=roles,
        coordination=coordination,
        role_template_digests=digests,
        source_path=path,
    )
    _verify_load_signature(data, manifest, context=context)
    return manifest


def parse_manifest_ref(ref: str) -> tuple[str, str | None]:
    """Split a ``<name>[@sha256]`` reference into ``(name, digest | None)``.

    The digest, when present, is normalized to lowercase.

    Raises:
        TeamManifestValidationError: If the reference is malformed.
    """
    match = _MANIFEST_REF_RE.match(ref.strip())
    if match is None:
        raise TeamManifestValidationError(
            f"invalid team_manifest reference {ref!r}; expected '<name>' or '<name>@<64-hex-sha256>'"
        )
    digest = match.group("digest")
    return match.group("name"), digest.lower() if digest else None


def team_manifest_search_dirs(workdir: Path) -> list[Path]:
    """Return the manifest search directories for *workdir*, in priority order.

    Mirrors :func:`bernstein.get_templates_dir`: the project-local
    ``.bernstein/templates`` wins, then ``<workdir>/templates``, then the
    package's bundled defaults.
    """
    dirs: list[Path] = []
    for base in (
        workdir / ".bernstein" / "templates",
        workdir / "templates",
        _BUNDLED_TEMPLATES_DIR,
    ):
        candidate = base / TEAM_MANIFEST_DIR_NAME
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def resolve_team_manifest(name: str, *, workdir: Path) -> TeamManifest:
    """Resolve a manifest by name against the search directories.

    Raises:
        TeamManifestNotFoundError: If no directory contains
            ``<name>.toml``.
        TeamManifestValidationError: If the winning file is invalid.
    """
    for directory in team_manifest_search_dirs(workdir):
        candidate = directory / f"{name}.toml"
        if candidate.is_file():
            return load_team_manifest(candidate)
    searched = ", ".join(str(d) for d in team_manifest_search_dirs(workdir))
    raise TeamManifestNotFoundError(f"team manifest {name!r} not found; searched: {searched}")


def discover_team_manifest_paths(workdir: Path) -> dict[str, Path]:
    """Return every discoverable manifest as ``name -> path``.

    Higher-priority directories shadow lower ones name-by-name, matching
    :func:`resolve_team_manifest`. Names are sorted for stable output.
    """
    found: dict[str, Path] = {}
    for directory in team_manifest_search_dirs(workdir):
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.toml")):
            found.setdefault(candidate.stem, candidate)
    return dict(sorted(found.items()))


def expand_manifest(manifest: TeamManifest) -> ExpandedTeam:
    """Expand a manifest into the existing seed team + policy structures.

    Pure and deterministic: the result depends only on the manifest's
    content, every call returns fresh objects, and the output feeds the
    standard ``role_model_policy`` validator so a manifest-driven seed is
    byte-identical to the equivalent hand-written one (AC1).
    """
    team = [role.role for role in manifest.roles]
    policy: dict[str, dict[str, str | int]] = {}
    for role in manifest.roles:
        entry: dict[str, str | int] = dict(role.model_policy)
        if role.response_profile is not None:
            entry["response_style"] = role.response_profile
        if entry:
            policy[role.role] = entry
    return ExpandedTeam(team=team, role_model_policy=policy)


def sign_team_manifest(manifest: TeamManifest, private_key_pem: str) -> str:
    """Sign the canonical serialization with an Ed25519 key.

    Thin wrapper over the skills catalog signing primitive so team
    manifests and skill manifests share one verification path.
    """
    return sign_payload(canonical_toml(manifest).encode("utf-8"), private_key_pem)


def verify_team_manifest(
    manifest: TeamManifest,
    signature: str | None,
    signer_pubkey: str | None,
    *,
    allow_unverified: bool = False,
) -> VerificationOutcome:
    """Verify a detached Ed25519 signature over the canonical serialization.

    Raises:
        ManifestSignatureError: If verification fails and
            ``allow_unverified`` is False.
    """
    return verify_payload(
        canonical_toml(manifest).encode("utf-8"),
        signature,
        signer_pubkey,
        allow_unverified=allow_unverified,
        missing_signature_reason="manifest has no signature",
        missing_key_reason="manifest has no signer_pubkey",
    )


__all__ = [
    "TEAM_MANIFEST_DIR_NAME",
    "ExpandedTeam",
    "TeamCoordination",
    "TeamManifest",
    "TeamManifestDigestMismatchError",
    "TeamManifestError",
    "TeamManifestNotFoundError",
    "TeamManifestSignatureError",
    "TeamManifestValidationError",
    "TeamRoleSpec",
    "canonical_toml",
    "discover_team_manifest_paths",
    "expand_manifest",
    "load_team_manifest",
    "parse_manifest_ref",
    "resolve_team_manifest",
    "sign_team_manifest",
    "team_manifest_search_dirs",
    "verify_team_manifest",
]
