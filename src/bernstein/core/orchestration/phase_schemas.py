"""Per-phase JSON schemas for the discrete-phase-separation pipeline.

The original phase pipeline shipped a single shared schema covering all
phases.  In practice that means an ``implement`` agent could emit a
``plan``-shaped artefact and the runner would happily forward it: the
phase boundary compresses *names* but not *content*.  We identified this
gap in our existing pipeline; this module replaces the shared shape
check with a strict per-phase contract.

Each phase declares its own ``additionalProperties: false`` schema with
explicit ``minLength`` / ``minItems`` constraints, validated with
``jsonschema.Draft202012Validator``.  Validation failures carry a
machine-parseable ``field_path`` so the boundary gates (see
:mod:`bernstein.core.orchestration.phase_gates`) can re-fire the failing
phase with the violation list pushed into ``open_questions``.

Capability-matrix integration
-----------------------------
Each phase's schema is also registered as a ``phase_emit:<phase>``
capability in :mod:`bernstein.core.security.capability_matrix`.  The
same machinery that gates the lethal trifecta therefore gates cross-
phase emission: an agent running under ``phase_emit:implement`` cannot
quietly emit a ``plan``-shaped artefact because the policy boundary
refuses the chain.  One subsystem, one audit surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError as _JSONSchemaValidationError

    from bernstein.core.orchestration.phase_pipeline import Phase
    from bernstein.core.security.capability_matrix import CapabilityRegistry


# ---------------------------------------------------------------------------
# Per-phase schemas
# ---------------------------------------------------------------------------


_COMMON_REQUIRED: tuple[str, ...] = ("summary", "decisions", "constraints", "open_questions")


def _base_artifact_schema() -> dict[str, Any]:
    """Build the shared scaffold every phase schema starts from.

    All four required fields are explicit and constrained.  Phases may
    extend the property set and tighten the constraints further by
    overlaying their own keys before sealing with
    ``additionalProperties: false``.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
            "decisions": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 200,
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 200,
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 200,
            },
        },
        "required": list(_COMMON_REQUIRED),
        "additionalProperties": False,
    }


def _research_schema() -> dict[str, Any]:
    """Research is the entry phase; ``open_questions`` may be non-empty."""
    schema = _base_artifact_schema()
    schema["$id"] = "bernstein://phase/research/v1"
    schema["title"] = "Research phase artefact"
    schema["properties"]["summary"]["minLength"] = 16
    return schema


def _plan_schema() -> dict[str, Any]:
    """Plan adds an explicit ``dependencies`` edge list.

    The edge list ``["a->b", "a->c"]`` form is what R003-acyclic-decision-graph
    consumes.  The graph form ``{"a": ["b", "c"]}`` would compress better
    but the edge list validates with stock jsonschema and consumers can
    reconstruct the adjacency map in one pass.
    """
    schema = _base_artifact_schema()
    schema["$id"] = "bernstein://phase/plan/v1"
    schema["title"] = "Plan phase artefact"
    schema["properties"]["summary"]["minLength"] = 16
    schema["properties"]["dependencies"] = {
        "type": "array",
        "items": {
            "type": "string",
            # Edge format: ``A->B`` (no spaces required, but tolerated).
            "pattern": r"^[^\s>]+\s*(->|→)\s*[^\s>]+$",
            "minLength": 3,
        },
        "maxItems": 500,
    }
    schema["required"] = [*_COMMON_REQUIRED, "dependencies"]
    return schema


def _implement_schema() -> dict[str, Any]:
    """Implement carries concrete deliverables the verifier asserts against."""
    schema = _base_artifact_schema()
    schema["$id"] = "bernstein://phase/implement/v1"
    schema["title"] = "Implement phase artefact"
    schema["properties"]["files_changed"] = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "maxItems": 1000,
    }
    schema["properties"]["tests_added"] = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "maxItems": 1000,
    }
    schema["properties"]["tests_passing"] = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "maxItems": 1000,
    }
    schema["required"] = [
        *_COMMON_REQUIRED,
        "files_changed",
        "tests_added",
        "tests_passing",
    ]
    return schema


def _verify_schema() -> dict[str, Any]:
    """Verify is terminal; require an explicit verdict field."""
    schema = _base_artifact_schema()
    schema["$id"] = "bernstein://phase/verify/v1"
    schema["title"] = "Verify phase artefact"
    schema["properties"]["verdict"] = {
        "type": "string",
        "enum": ["pass", "fail", "partial"],
    }
    schema["required"] = [*_COMMON_REQUIRED, "verdict"]
    return schema


RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = _research_schema()
PLAN_OUTPUT_SCHEMA: dict[str, Any] = _plan_schema()
IMPLEMENT_OUTPUT_SCHEMA: dict[str, Any] = _implement_schema()
VERIFY_OUTPUT_SCHEMA: dict[str, Any] = _verify_schema()


_SCHEMA_BY_PHASE_NAME: dict[str, dict[str, Any]] = {
    "research": RESEARCH_OUTPUT_SCHEMA,
    "plan": PLAN_OUTPUT_SCHEMA,
    "implement": IMPLEMENT_OUTPUT_SCHEMA,
    "verify": VERIFY_OUTPUT_SCHEMA,
}


def schema_for_phase(phase: Phase | str) -> dict[str, Any]:
    """Return the strict output schema for *phase*.

    Accepts either a :class:`Phase` enum or its string value so callers
    in plain-text contexts (template renderers, CLI tools) do not need
    to import the enum.

    Raises:
        KeyError: If *phase* is not a recognised phase name.
    """
    name = phase.value if hasattr(phase, "value") else str(phase)
    return _SCHEMA_BY_PHASE_NAME[name]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseSchemaError:
    """One validation failure with a machine-parseable address.

    ``field_path`` is the slash-joined absolute path inside the artefact
    payload (``"decisions/3"`` for the fourth list element, ``"summary"``
    for the top-level field).  Empty path means the failure was at the
    root object level (e.g. ``additionalProperties`` rejection).
    """

    phase: str
    schema_id: str
    field_path: str
    message: str


def _format_path(error: _JSONSchemaValidationError) -> str:
    return "/".join(str(p) for p in error.absolute_path)


def validate_phase_output(phase: Phase | str, payload: object) -> list[PhaseSchemaError]:
    """Validate *payload* against the per-phase schema.

    Returns a list rather than raising so the caller can decide whether
    to short-circuit on the first error or surface the full set to the
    re-fire seed. An empty list means the payload is valid.

    Invalid input produces :class:`PhaseSchemaError` entries. An
    unrecognized phase produces exactly one root-level error: ``phase``
    contains the supplied phase name and ``schema_id`` and ``field_path``
    are empty. For a recognized phase with a non-object payload, validation
    produces exactly one root-level error with the selected schema's
    ``schema_id``. For object payloads, validation errors are ordered by
    their absolute field paths, lexicographically by path components;
    ``field_path`` is the slash-joined path.

    Args:
        phase: Phase whose schema to use.
        payload: Decoded artefact (mapping).  Non-mapping inputs yield a
            single root-level error rather than a confusing validator
            traceback.
    """
    name = phase.value if hasattr(phase, "value") else str(phase)
    schema = _SCHEMA_BY_PHASE_NAME.get(name)
    if schema is None:
        return [
            PhaseSchemaError(
                phase=name,
                schema_id="",
                field_path="",
                message=f"unknown phase: {name}",
            )
        ]
    schema_id = str(schema.get("$id", ""))
    if not isinstance(payload, dict):
        return [
            PhaseSchemaError(
                phase=name,
                schema_id=schema_id,
                field_path="",
                message=f"phase artefact must be an object, got {type(payload).__name__}",
            )
        ]

    validator = Draft202012Validator(schema)
    errors: list[PhaseSchemaError] = [
        PhaseSchemaError(
            phase=name,
            schema_id=schema_id,
            field_path=_format_path(err),
            message=err.message,
        )
        for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]
    return errors


class PhaseValidationError(ValueError):
    """Raised when a phase artefact fails its declared schema.

    Attributes:
        phase: Phase whose schema rejected the payload.
        schema_id: Stable ``$id`` of the failing schema (for audit).
        field_path: First failing field path (``""`` if the failure was
            at the root object).
        errors: Full ordered list of :class:`PhaseSchemaError` entries -
            preserved so the boundary-gate retry path can seed every
            violation into ``open_questions`` rather than only the first.
    """

    def __init__(self, phase: str, errors: list[PhaseSchemaError]) -> None:
        self.phase = phase
        self.errors = errors
        first = errors[0] if errors else None
        self.schema_id = first.schema_id if first is not None else ""
        self.field_path = first.field_path if first is not None else ""
        message = (
            f"phase {phase!r} artefact failed schema validation: {first.field_path or '<root>'}: {first.message}"
            if first is not None
            else f"phase {phase!r} artefact failed schema validation"
        )
        super().__init__(message)


# ---------------------------------------------------------------------------
# Capability-matrix integration
# ---------------------------------------------------------------------------


PHASE_EMIT_CAPABILITY_PREFIX = "phase_emit:"


def phase_emit_capability(phase: Phase | str) -> str:
    """Return the canonical capability name for *phase* emission."""
    name = phase.value if hasattr(phase, "value") else str(phase)
    return f"{PHASE_EMIT_CAPABILITY_PREFIX}{name}"


def register_with_capability_matrix(registry: CapabilityRegistry) -> list[str]:
    """Register each phase's schema as a capability on *registry*.

    The matrix's existing ``ToolCapabilities`` entry shape is used: each
    phase is a "tool" (the agent's emission act for that phase) carrying
    the empty capability set, so cross-phase emission is gated by a
    direct presence check rather than the lethal-trifecta union.

    Returns the list of registered tool names so callers (tests, CLI
    audit) can confirm the bindings without reaching into the registry.
    """
    # Local import: capability_matrix is a peer subsystem and we keep the
    # import narrow to avoid orchestration → security top-level cycles.
    from bernstein.core.security.capability_matrix import (
        ToolCapabilities,
    )

    registered: list[str] = []
    for phase_name, schema in _SCHEMA_BY_PHASE_NAME.items():
        tool_name = f"{PHASE_EMIT_CAPABILITY_PREFIX}{phase_name}"
        registry.register(
            ToolCapabilities(
                tool_name=tool_name,
                capabilities=frozenset(),
                source="declared",
            )
        )
        registered.append(tool_name)
        # Also stash the schema id under the tool name for audit lookups
        # - the matrix only stores capability sets, but this index lets
        # external callers prove which schema gate was active.
        _PHASE_EMIT_SCHEMA_INDEX[tool_name] = str(schema.get("$id", ""))
    return registered


_PHASE_EMIT_SCHEMA_INDEX: dict[str, str] = {}


def phase_emit_schema_id(tool_name: str) -> str | None:
    """Return the schema id bound to *tool_name* by the last registration call."""
    return _PHASE_EMIT_SCHEMA_INDEX.get(tool_name)


def assert_phase_emission_allowed(
    registry: CapabilityRegistry,
    declared_phase: Phase | str,
    emitted_phase: Phase | str,
) -> None:
    """Raise :class:`PhaseValidationError` if *emitted_phase* != *declared_phase*.

    Cross-phase emission is the failure mode this gate exists for: an
    ``implement`` agent attempting to emit a ``plan`` artefact must be
    refused at the policy boundary, not silently downstream.

    Args:
        registry: Capability registry; the phase capabilities must already
            be registered (see :func:`register_with_capability_matrix`).
        declared_phase: Phase the agent was spawned for.
        emitted_phase: Phase the artefact actually claims to be.
    """
    declared_name = declared_phase.value if hasattr(declared_phase, "value") else str(declared_phase)
    emitted_name = emitted_phase.value if hasattr(emitted_phase, "value") else str(emitted_phase)
    declared_tool = f"{PHASE_EMIT_CAPABILITY_PREFIX}{declared_name}"
    emitted_tool = f"{PHASE_EMIT_CAPABILITY_PREFIX}{emitted_name}"
    declared_entry = registry.tools.get(declared_tool)
    emitted_entry = registry.tools.get(emitted_tool)
    if declared_entry is None or emitted_entry is None:
        raise PhaseValidationError(
            emitted_name,
            [
                PhaseSchemaError(
                    phase=emitted_name,
                    schema_id="",
                    field_path="",
                    message=(
                        f"phase emission capability not registered: declared={declared_tool} emitted={emitted_tool}"
                    ),
                )
            ],
        )
    if declared_name != emitted_name:
        raise PhaseValidationError(
            emitted_name,
            [
                PhaseSchemaError(
                    phase=emitted_name,
                    schema_id=phase_emit_schema_id(emitted_tool) or "",
                    field_path="",
                    message=(
                        f"cross-phase emission denied: agent declared {declared_tool!r} "
                        f"but artefact claims {emitted_tool!r}"
                    ),
                )
            ],
        )


__all__ = [
    "IMPLEMENT_OUTPUT_SCHEMA",
    "PHASE_EMIT_CAPABILITY_PREFIX",
    "PLAN_OUTPUT_SCHEMA",
    "RESEARCH_OUTPUT_SCHEMA",
    "VERIFY_OUTPUT_SCHEMA",
    "PhaseSchemaError",
    "PhaseValidationError",
    "assert_phase_emission_allowed",
    "phase_emit_capability",
    "phase_emit_schema_id",
    "register_with_capability_matrix",
    "schema_for_phase",
    "validate_phase_output",
]
