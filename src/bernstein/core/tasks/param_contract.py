"""Shared typed parameter contract for the input side of the run boundary.

Issue #2545. Worker terminal payloads are schema validated with a closed
taxonomy and JSONPath diagnostics (``core/tasks/contracts.py``), MCP tool calls
pass a deny-by-default input firewall, and recipes declare typed params. The
*input* side of a fire / claim / launch had no equivalent surface. This module
is that surface: the ``RecipeParam`` vocabulary (name, type, default, required,
choices) extracted into one place, plus a content-addressed ``params_hash`` so
a validated parameter map becomes a hash that lands inside the deterministic
fire projection.

The three load-bearing properties:

* **One vocabulary.** :class:`ParamSpec` is the single declaration of a typed
  parameter. ``core/workflows/recipe_spec.py`` reuses :data:`ParamType` and
  :func:`coerce_value` from here rather than defining its own, and a sibling
  registered-recipes surface (#2546) imports the same :class:`ParamContract`.

* **Canonical hashing.** :meth:`ParamContract.params_hash` folds the declared
  type and the coerced value of every parameter into one ``sha256:`` digest
  using the same canonical-encoding discipline as
  ``core/orchestration/schedule_projection.py`` (sorted keys, minimal
  separators, ``allow_nan=False``). Two operators with equal validated maps
  derive the byte-identical hash; changing any single value changes it.

* **Refusal with a JSONPath.** A value that fails its contract raises
  :class:`ParamContractViolation` carrying the JSONPath of the offending field
  (the same diagnosability convention as ``ContractViolation``), the declared
  schema hash, and a digest of the rejected value -- never the raw bytes. That
  is exactly the payload a signed input-refusal receipt binds.

Determinism contract (do not relax): :func:`coerce_value`,
:meth:`ParamContract.params_hash`, and :meth:`ParamContract.schema_hash` are
pure. No clock, no randomness, no host-dependent ordering, no model. Two
verifiers on different machines recompute byte-identical hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

__all__ = [
    "PARAM_CONTRACT_HASH_REV",
    "PARAM_TYPE_VOCABULARY",
    "ParamCoercionError",
    "ParamContract",
    "ParamContractError",
    "ParamContractViolation",
    "ParamSpec",
    "ParamType",
    "ParamValue",
    "coerce_value",
    "value_digest",
]

#: Rev marker baked into every ``params_hash`` preimage. Bumping it changes
#: every params hash and is the single source of truth for when the input-side
#: canonical encoding is allowed to evolve, mirroring
#: ``SCHEDULE_PROJECTION_REV`` on the projection side.
PARAM_CONTRACT_HASH_REV = "1"

#: The supported scalar types. Kept small on purpose: parameters are
#: operator-facing, not a general-purpose templating engine. Mirrors the recipe
#: parameter vocabulary so the two surfaces cannot diverge.
ParamType = Literal["string", "int", "float", "bool"]

#: The type vocabulary as data, for property tests that sweep every type.
PARAM_TYPE_VOCABULARY: tuple[ParamType, ...] = ("string", "int", "float", "bool")

#: The coerced value space of a parameter.
ParamValue = str | int | float | bool

# Parameter names are slug-shaped so they round-trip through YAML keys and CLI
# flags without escaping, matching the recipe param name constraint.
_PARAM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ParamContractError(ValueError):
    """Raised when a parameter *schema* is malformed (bad declaration)."""


class ParamCoercionError(ParamContractError):
    """Raised when a raw value cannot be coerced to its declared type.

    A subtype of :class:`ParamContractError` so a caller that only cares that
    "the schema layer rejected this" can catch the base class, while the fire /
    claim boundary can catch this specific type to attach a JSONPath.
    """


class ParamContractViolation(ValueError):
    """Raised when operator-provided values fail a declared contract.

    Carries the same diagnosability payload a signed input-refusal receipt
    binds: the JSONPath of the offending field, the declared schema hash, and a
    digest of the rejected value (raw bytes never stored).

    Attributes:
        json_path: JSONPath of the offending field, e.g. ``$.params.target``.
        schema_hash: ``sha256:`` hash of the declared parameter schema.
        value_digest: ``sha256:`` digest of the rejected value (or ``""`` when
            the violation is a missing / unknown field with no value to digest).
        reason_code: Machine-stable reason (``unknown_param``, ``missing_required``,
            ``bad_type``, ``bad_choice``).
    """

    def __init__(
        self,
        message: str,
        *,
        json_path: str,
        schema_hash: str,
        value_digest: str = "",
        reason_code: str = "invalid",
    ) -> None:
        super().__init__(message)
        self.json_path = json_path
        self.schema_hash = schema_hash
        self.value_digest = value_digest
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Coercion (pure)
# ---------------------------------------------------------------------------


def coerce_value(raw: str, type_: ParamType) -> ParamValue:
    """Coerce a raw string into the declared scalar type.

    Args:
        raw: Raw string value as it appeared on the command line / in config.
        type_: Declared parameter type.

    Returns:
        The coerced Python value.

    Raises:
        ParamCoercionError: When ``raw`` does not parse as the declared type.
            Bool accepts the usual truthy / falsy spellings.
    """
    if type_ == "string":
        return raw
    if type_ == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ParamCoercionError(f"expected int, got {raw!r}") from exc
    if type_ == "float":
        try:
            parsed = float(raw)
        except ValueError as exc:
            raise ParamCoercionError(f"expected float, got {raw!r}") from exc
        # NaN / +-Infinity serialise with a non-portable json extension token
        # and would fork two operators' params hashes; reject at coercion time.
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            raise ParamCoercionError(f"non-finite float not permitted: {raw!r}")
        return parsed
    if type_ == "bool":
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
        raise ParamCoercionError(f"expected bool, got {raw!r}")
    raise ParamContractError(f"unsupported type {type_!r}")  # pragma: no cover


def value_digest(value: Any) -> str:
    """Return a ``sha256:`` digest of a rejected value; raw bytes never stored.

    The value is canonicalised (sorted, minimal separators) before hashing so a
    verifier can recompute the same digest from the same offending input while
    the receipt never carries the value itself.
    """
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Parameter spec (the one vocabulary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One typed, operator-facing parameter declaration.

    The canonical vocabulary shared by recipes (#2478) and schedules (#2545).
    Frozen + slots so the declaration cannot mutate under a caller.

    Attributes:
        name: Slug-shaped parameter name.
        type: One of :data:`PARAM_TYPE_VOCABULARY`.
        default: Optional default applied when the operator omits the value.
        required: When True, a missing value is a violation rather than a
            fallback to ``default`` / empty.
        help: One-line operator-facing description.
        choices: Optional whitelist for ``string`` params.
    """

    name: str
    type: ParamType = "string"
    default: ParamValue | None = None
    required: bool = False
    help: str = ""
    choices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not _PARAM_NAME_PATTERN.match(self.name):
            raise ParamContractError(f"param name {self.name!r} must match {_PARAM_NAME_PATTERN.pattern}")
        if self.name == "goal":
            # ``goal`` is substituted by the runner / projection itself, not by
            # the param layer; a collision would silently shadow operator input.
            raise ParamContractError("param name 'goal' is reserved")
        if self.type not in PARAM_TYPE_VOCABULARY:
            raise ParamContractError(f"param {self.name!r} has unsupported type {self.type!r}")
        if self.choices is not None and self.type != "string":
            raise ParamContractError(
                f"param {self.name!r} declares choices but type is {self.type!r}; choices only apply to 'string'"
            )
        if self.default is not None:
            try:
                coerce_value(str(self.default), self.type)
            except ParamCoercionError as exc:
                raise ParamContractError(f"default for {self.name!r}: {exc}") from exc
            if self.choices is not None and str(self.default) not in self.choices:
                raise ParamContractError(f"default {self.default!r} for {self.name!r} not in choices {self.choices!r}")

    def to_schema_dict(self) -> dict[str, Any]:
        """Return the canonical declaration dict (order-stable, no ``None`` noise)."""
        out: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.default is not None:
            out["default"] = self.default
        if self.help:
            out["help"] = self.help
        if self.choices is not None:
            out["choices"] = list(self.choices)
        return out

    @classmethod
    def from_schema_dict(cls, row: Mapping[str, Any]) -> ParamSpec:
        """Build a spec from a declaration mapping (config / manifest / store)."""
        if not isinstance(row, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ParamContractError(f"param declaration must be a mapping, got {type(row).__name__}")
        name = row.get("name")
        if not isinstance(name, str):
            raise ParamContractError("param declaration missing string 'name'")
        # ``type`` is validated against the vocabulary in ``__post_init__``; keep
        # the raw declared value here so a bad type surfaces as a
        # ``ParamContractError`` naming the offending value rather than a silent
        # coercion. Cast narrows ``Any`` for the typed field.
        raw_type = cast("ParamType", row.get("type", "string"))
        choices_raw = row.get("choices")
        choices = tuple(str(c) for c in choices_raw) if isinstance(choices_raw, (list, tuple)) else None
        return cls(
            name=name,
            type=raw_type,
            default=row.get("default"),
            required=bool(row.get("required", False)),
            help=str(row.get("help", "")),
            choices=choices,
        )


# ---------------------------------------------------------------------------
# Parameter contract (a set of specs + validation + hashing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParamContract:
    """An ordered set of :class:`ParamSpec`, with validation and hashing.

    The contract is the thing a schedule / recipe / registered recipe declares.
    :meth:`validate_and_coerce` turns raw operator values into a canonical
    validated map (or raises :class:`ParamContractViolation`);
    :meth:`params_hash` folds that map into one content hash.
    """

    specs: tuple[ParamSpec, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for spec in self.specs:
            if spec.name in seen:
                raise ParamContractError(f"duplicate param name {spec.name!r}")
            seen.add(spec.name)

    @property
    def is_empty(self) -> bool:
        """True when no parameters are declared (the backward-compatible case)."""
        return not self.specs

    @classmethod
    def from_schema(cls, schema: object) -> ParamContract:
        """Build a contract from a list of declaration mappings.

        A ``None`` / empty schema yields the empty contract, so a manifest or
        schedule without a params block round-trips unchanged (AC5).
        """
        if not schema:
            return cls(())
        if not isinstance(schema, (list, tuple)):
            raise ParamContractError("params schema must be a list of parameter declarations")
        specs = tuple(ParamSpec.from_schema_dict(row) for row in schema)
        return cls(specs)

    def to_schema(self) -> list[dict[str, Any]]:
        """Return the canonical schema as a list of declaration dicts."""
        return [spec.to_schema_dict() for spec in self.specs]

    def _type_of(self, name: str) -> ParamType:
        for spec in self.specs:
            if spec.name == name:
                return spec.type
        raise KeyError(name)  # pragma: no cover - callers pass validated names

    def schema_hash(self) -> str:
        """Return the ``sha256:`` content hash of the declared schema.

        A caller binds this into a refusal receipt so a verifier can prove the
        rejection was evaluated against a specific declared contract. The empty
        contract has a stable hash of its own (an empty spec list).
        """
        canonical = json.dumps(
            {"v": PARAM_CONTRACT_HASH_REV, "specs": self.to_schema()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def validate_and_coerce(self, raw_values: Mapping[str, Any]) -> dict[str, ParamValue]:
        """Validate and coerce ``raw_values`` against this contract.

        Args:
            raw_values: Operator-supplied values keyed by param name. Values may
                arrive as strings (CLI / config) or already-typed scalars
                (JSON); both are coerced through the declared type.

        Returns:
            Mapping from parameter name to coerced value, including defaulted
            entries for omitted params.

        Raises:
            ParamContractViolation: On an unknown param, a missing required
                param, a choice violation, or a coercion failure. The raised
                error carries the JSONPath of the offending field, this
                contract's schema hash, and a digest of the rejected value.
        """
        schema_hash = self.schema_hash()
        declared = {s.name for s in self.specs}
        unknown = sorted(set(raw_values) - declared)
        if unknown:
            offender = unknown[0]
            raise ParamContractViolation(
                f"unknown param(s): {', '.join(unknown)}; declared: {sorted(declared) or '(none)'}",
                json_path=f"$.params.{offender}",
                schema_hash=schema_hash,
                value_digest=value_digest(raw_values[offender]),
                reason_code="unknown_param",
            )

        resolved: dict[str, ParamValue] = {}
        for spec in self.specs:
            json_path = f"$.params.{spec.name}"
            if spec.name in raw_values:
                raw = raw_values[spec.name]
                try:
                    value = coerce_value(str(raw), spec.type)
                except ParamCoercionError as exc:
                    raise ParamContractViolation(
                        f"{spec.name}: {exc}",
                        json_path=json_path,
                        schema_hash=schema_hash,
                        value_digest=value_digest(raw),
                        reason_code="bad_type",
                    ) from exc
                if spec.choices is not None and str(value) not in spec.choices:
                    raise ParamContractViolation(
                        f"{spec.name}={value!r} not in choices {list(spec.choices)!r}",
                        json_path=json_path,
                        schema_hash=schema_hash,
                        value_digest=value_digest(raw),
                        reason_code="bad_choice",
                    )
                resolved[spec.name] = value
                continue
            if spec.default is not None:
                resolved[spec.name] = spec.default
                continue
            if spec.required:
                raise ParamContractViolation(
                    f"missing required param {spec.name!r}",
                    json_path=json_path,
                    schema_hash=schema_hash,
                    value_digest="",
                    reason_code="missing_required",
                )
        return resolved

    def params_hash(self, validated: Mapping[str, ParamValue]) -> str:
        """Return the ``sha256:`` content hash of a validated parameter map.

        The preimage folds each parameter's declared *type* and coerced *value*
        (sorted by name) so ``"1"`` (string), ``1`` (int), and ``True`` (bool)
        never collide, and so a changed value provably changes the hash. Two
        operators with equal validated maps derive the byte-identical hash.

        An empty map hashes to a fixed sentinel, so a params-less fire is
        distinguishable from a fire whose params happened to hash to empty.
        """
        entries: list[list[Any]] = []
        for name in sorted(validated):
            try:
                type_ = self._type_of(name)
            except KeyError:  # pragma: no cover - validated maps only carry declared names
                type_ = "string"
            entries.append([name, type_, validated[name]])
        canonical = json.dumps(
            {"v": PARAM_CONTRACT_HASH_REV, "params": entries},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def validated_hash(self, raw_values: Mapping[str, Any]) -> tuple[dict[str, ParamValue], str]:
        """Convenience: validate + coerce, then hash. Returns ``(validated, hash)``."""
        validated = self.validate_and_coerce(raw_values)
        return validated, self.params_hash(validated)
