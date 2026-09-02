"""Governance playbook schema and validation.

A governance playbook defines the policy surface for an orchestration run:
which surfaces (resources, actions) are in scope and which ceilings
(budgets, limits) constrain them. Clauses bind surfaces to ceilings with
a free-text reason for audit provenance.

This module provides:
- :class:`Surface` - a resource or action reference
- :class:`Ceiling` - a named budget or limit definition
- :class:`GovernanceClause` - a binding of surface to ceiling with reason
- :class:`GovernancePlaybook` - top-level playbook container
- :class:`PlaybookSchema` - validator that checks referential integrity
- :class:`PlaybookValidationError` - structured validation error

Example playbook::

    playbook_id: pb-001
    name: "Production limits"
    description: "Budget and access controls for production runs"
    version: "1.0.0"
    surfaces:
      - surface_id: prod-api
        kind: api_endpoint
        selector: "api.production/*"
      - surface_id: prod-db
        kind: database
        selector: "db.production.main"
    ceilings:
      - ceiling_id: max-cost
        kind: budget
        limit: "100.00 USD"
      - ceiling_id: rate-limit
        kind: rate
        limit: "100 req/min"
    clauses:
      - surface_ref: prod-api
        ceiling_ref: max-cost
        reason: "API calls must not exceed production budget"
      - surface_ref: prod-api
        ceiling_ref: rate-limit
        reason: "Rate limit to prevent overload"
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# ---------------------------------------------------------------------------
# Identifier constraints
# ---------------------------------------------------------------------------

# Surface and ceiling ids are slug-shaped: lowercase letters, digits,
# underscores, hyphens. Must start with a letter.
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

# Permissive semver-ish: MAJOR.MINOR[.PATCH] plus optional pre-release.
_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.-]+)?$")


class PlaybookValidationError(ValueError):
    """Raised when a governance playbook fails validation.

    Carries structured error details for CLI rendering.

    Attributes:
        errors: List of (field, message) tuples describing each violation.
    """

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = errors
        formatted = "; ".join(f"{field}: {msg}" for field, msg in errors)
        super().__init__(formatted)

    def to_json(self) -> list[dict[str, str]]:
        """Return errors as JSON-serializable list."""
        return [{"field": field, "message": msg} for field, msg in self.errors]


# ---------------------------------------------------------------------------
# Component models
# ---------------------------------------------------------------------------


class Surface(BaseModel):
    """A resource or action reference in the governance surface.

    Attributes:
        surface_id: Unique identifier for this surface.
        kind: Category of surface (e.g., api_endpoint, database, action).
        selector: Expression that matches concrete resources.
        description: Optional human-readable description.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    selector: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("surface_id")
    @classmethod
    def _check_surface_id(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError(f"surface_id {value!r} must match pattern {_ID_PATTERN.pattern}")
        return value


class Ceiling(BaseModel):
    """A named budget or limit definition.

    Attributes:
        ceiling_id: Unique identifier for this ceiling.
        kind: Category of ceiling (e.g., budget, rate, count).
        limit: The limit value (e.g., "100.00 USD", "100 req/min").
        description: Optional human-readable description.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ceiling_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    limit: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("ceiling_id")
    @classmethod
    def _check_ceiling_id(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError(f"ceiling_id {value!r} must match pattern {_ID_PATTERN.pattern}")
        return value


class GovernanceClause(BaseModel):
    """A binding of a surface to a ceiling with a reason.

    Attributes:
        surface_ref: Reference to a :class:`Surface.surface_id`.
        ceiling_ref: Reference to a :class:`Ceiling.ceiling_id`.
        reason: Free-text justification for audit provenance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_ref: str = Field(min_length=1, max_length=64)
    ceiling_ref: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1024)

    @field_validator("surface_ref")
    @classmethod
    def _check_surface_ref(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError(f"surface_ref {value!r} must match pattern {_ID_PATTERN.pattern}")
        return value

    @field_validator("ceiling_ref")
    @classmethod
    def _check_ceiling_ref(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError(f"ceiling_ref {value!r} must match pattern {_ID_PATTERN.pattern}")
        return value


# ---------------------------------------------------------------------------
# Top-level playbook
# ---------------------------------------------------------------------------


class GovernancePlaybook(BaseModel):
    """Top-level governance playbook container.

    A playbook defines the policy surface for an orchestration run:
    which surfaces are in scope, which ceilings constrain them, and
    which clauses bind surfaces to ceilings.

    Attributes:
        playbook_id: Unique identifier for this playbook.
        name: Human-readable name.
        description: One-line description of the playbook's purpose.
        version: Semver-ish version string.
        surfaces: List of :class:`Surface` definitions.
        ceilings: List of :class:`Ceiling` definitions.
        clauses: List of :class:`GovernanceClause` bindings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    playbook_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    version: str = Field(default="1.0.0")
    surfaces: list[Surface] = Field(default_factory=list)
    ceilings: list[Ceiling] = Field(default_factory=list)
    clauses: list[GovernanceClause] = Field(default_factory=list)

    @field_validator("playbook_id")
    @classmethod
    def _check_playbook_id(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError(f"playbook_id {value!r} must match pattern {_ID_PATTERN.pattern}")
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not _VERSION_PATTERN.match(value):
            raise ValueError(f"version {value!r} must look like '1.2' or '1.2.3'")
        return value


# ---------------------------------------------------------------------------
# Schema validator
# ---------------------------------------------------------------------------


class PlaybookSchema:
    """Validator for governance playbook referential integrity.

    Checks that:
    - All surface_refs in clauses resolve to known surfaces
    - All ceiling_refs in clauses resolve to defined ceilings
    - No duplicate surface_id assignments
    - No duplicate ceiling_id assignments
    - Every clause has a non-empty reason

    Example::

        schema = PlaybookSchema()
        try:
            schema.validate(playbook)
        except PlaybookValidationError as e:
            for field, msg in e.errors:
                print(f"{field}: {msg}")
    """

    def validate(self, playbook: GovernancePlaybook) -> None:
        """Validate referential integrity of a playbook.

        Args:
            playbook: The playbook to validate.

        Raises:
            PlaybookValidationError: When validation fails.
        """
        errors: list[tuple[str, str]] = []

        # Check for duplicate surface_ids
        surface_ids: set[str] = set()
        for surface in playbook.surfaces:
            if surface.surface_id in surface_ids:
                errors.append(
                    (
                        f"surfaces.{surface.surface_id}",
                        f"duplicate surface_id {surface.surface_id!r}",
                    )
                )
            surface_ids.add(surface.surface_id)

        # Check for duplicate ceiling_ids
        ceiling_ids: set[str] = set()
        for ceiling in playbook.ceilings:
            if ceiling.ceiling_id in ceiling_ids:
                errors.append(
                    (
                        f"ceilings.{ceiling.ceiling_id}",
                        f"duplicate ceiling_id {ceiling.ceiling_id!r}",
                    )
                )
            ceiling_ids.add(ceiling.ceiling_id)

        # Check clause references
        for idx, clause in enumerate(playbook.clauses):
            # surface_ref must resolve
            if clause.surface_ref not in surface_ids:
                errors.append(
                    (
                        f"clauses.{idx}.surface_ref",
                        f"surface_ref {clause.surface_ref!r} does not resolve to any defined surface",
                    )
                )

            # ceiling_ref must resolve
            if clause.ceiling_ref not in ceiling_ids:
                errors.append(
                    (
                        f"clauses.{idx}.ceiling_ref",
                        f"ceiling_ref {clause.ceiling_ref!r} does not resolve to any defined ceiling",
                    )
                )

            # reason must be non-empty (already enforced by Pydantic Field, but
            # we check explicitly for clarity)
            if not clause.reason or not clause.reason.strip():
                errors.append(
                    (
                        f"clauses.{idx}.reason",
                        "reason field must be non-empty",
                    )
                )

        if errors:
            raise PlaybookValidationError(errors)


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------


def load_playbook_from_text(text: str) -> GovernancePlaybook:
    """Parse text as YAML and coerce into a :class:`GovernancePlaybook`.

    Args:
        text: Raw playbook YAML text.

    Returns:
        A validated :class:`GovernancePlaybook`.

    Raises:
        PlaybookValidationError: When YAML is malformed or validation fails.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PlaybookValidationError([("yaml", f"malformed YAML: {exc}")]) from exc

    if not isinstance(data, dict):
        raise PlaybookValidationError([("structure", "playbook must be a mapping at the top level")])

    try:
        return GovernancePlaybook.model_validate(data)
    except ValidationError as exc:
        raise PlaybookValidationError([("schema", str(exc))]) from exc


def load_playbook(path: Any) -> GovernancePlaybook:
    """Load a governance playbook from a file path.

    Args:
        path: Path to the playbook YAML file.

    Returns:
        A validated :class:`GovernancePlaybook`.

    Raises:
        PlaybookValidationError: When the file is missing or invalid.
    """
    from pathlib import Path

    p = Path(path) if not isinstance(path, Path) else path

    if not p.is_file():
        raise PlaybookValidationError([("file", f"playbook not found: {p}")])

    return load_playbook_from_text(p.read_text(encoding="utf-8"))
