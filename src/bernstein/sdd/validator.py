"""JSON-Schema validator for SDD ticket frontmatter.

The schema lives next to this module under ``bernstein.sdd.schema`` and ships
in the wheel. It is loaded via :mod:`importlib.resources` so installed packages
work without a source checkout.

Public surface:

- :class:`ValidationReport`  - per-file result.
- :class:`ValidationIssue`   - single error / warning entry.
- :func:`validate_ticket`    - validate one file on disk.
- :func:`validate_ticket_metadata`  - validate an already-parsed mapping.
- :func:`load_schema`        - load a packaged schema by version label.
- :class:`SchemaNotFoundError`  - raised when an unknown version is requested.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml
from jsonschema import FormatChecker

__all__ = [
    "RECOMMENDED_KEYS",
    "SchemaNotFoundError",
    "ValidationIssue",
    "ValidationReport",
    "list_recommended_keys",
    "load_schema",
    "parse_frontmatter",
    "validate_ticket",
    "validate_ticket_metadata",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Recommended (non-required) keys. Missing keys produce warnings, not errors.
RECOMMENDED_KEYS: tuple[str, ...] = (
    "owner",
    "success_metric",
    "acceptance_criteria",
    "evidence",
    "risk",
    "rice",
    "ladder_to",
)

_SCHEMA_PACKAGE = "bernstein.sdd.schema"
_SCHEMA_FILENAME_TEMPLATE = "ticket.{version}.json"
_FRONTMATTER_DELIM = "---"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaNotFoundError(LookupError):
    """Raised when the requested schema version is not packaged."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue (error or warning)."""

    message: str
    path: tuple[str | int, ...] = ()
    code: str = ""

    def render(self) -> str:
        """Return a human-readable single line."""
        prefix = ""
        if self.path:
            prefix = ".".join(str(p) for p in self.path) + ": "
        return f"{prefix}{self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "path": list(self.path),
            "code": self.code,
        }


@dataclass
class ValidationReport:
    """Per-file validation report."""

    path: Path
    errors: list[ValidationIssue] = field(default_factory=list[ValidationIssue])
    warnings: list[ValidationIssue] = field(default_factory=list[ValidationIssue])

    @property
    def ok(self) -> bool:
        """True iff there are no errors."""
        return not self.errors

    @property
    def status(self) -> str:
        if self.errors:
            return "fail"
        if self.warnings:
            return "warn"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "status": self.status,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }

    @classmethod
    def error(cls, path: Path, message: str, code: str = "") -> ValidationReport:
        """Build a report containing a single top-level error."""
        return cls(path=path, errors=[ValidationIssue(message=message, code=code or "parse_error")])


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_VERSION_RE = re.compile(r"^v\d+(?:\.\d+)?$", re.ASCII)


def _coerce_version(version: str) -> str:
    """Normalize a version label. Strict: must look like ``v1`` or ``v1.2``."""
    if not version:
        raise SchemaNotFoundError(f"invalid schema version: {version!r}")
    if not _VERSION_RE.match(version):
        raise SchemaNotFoundError(f"invalid schema version: {version!r}")
    return version


def load_schema(version: str = "v1") -> dict[str, Any]:
    """Load the packaged JSON schema for *version* (e.g. ``"v1"``).

    Raises :class:`SchemaNotFoundError` if the file is not packaged.
    """
    canonical = _coerce_version(version)
    cached = _SCHEMA_CACHE.get(canonical)
    if cached is not None:
        return cached
    filename = _SCHEMA_FILENAME_TEMPLATE.format(version=canonical)
    try:
        resource = resources.files(_SCHEMA_PACKAGE).joinpath(filename)
        if not resource.is_file():
            raise SchemaNotFoundError(f"schema not found: {filename}")
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise SchemaNotFoundError(f"schema not found: {filename}") from exc
    try:
        loaded_raw: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaNotFoundError(f"schema {filename} is not valid JSON: {exc}") from exc
    if not isinstance(loaded_raw, dict):
        raise SchemaNotFoundError(f"schema {filename} is not a JSON object")
    loaded: dict[str, Any] = cast("dict[str, Any]", loaded_raw).copy()
    # Sanity-check it really is Draft-07 compatible.
    jsonschema.Draft7Validator.check_schema(loaded)
    _SCHEMA_CACHE[canonical] = loaded
    return loaded


def list_recommended_keys(version: str = "v1") -> tuple[str, ...]:
    """Return the recommended-key list for *version*. Currently version-agnostic."""
    _coerce_version(version)
    return RECOMMENDED_KEYS


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Extract a YAML frontmatter mapping from *text*.

    Supports three shapes:

    1. Markdown with leading ``---`` fenced frontmatter.
    2. A ``.yaml`` file whose body is a single mapping (no fences).
    3. A ``.yaml`` file that opens with ``---`` then a mapping.

    Returns ``None`` if no mapping could be extracted.
    """
    if not text:
        return None
    stripped = text.lstrip("﻿")  # drop BOM if any
    # Case 1 / 3: fenced frontmatter at top of file.
    if stripped.startswith(_FRONTMATTER_DELIM):
        # Use splitlines to find the closing fence.
        lines = stripped.splitlines()
        # Strip the opening fence line (it may contain trailing chars).
        if lines and lines[0].strip() == _FRONTMATTER_DELIM:
            body_lines: list[str] = []
            closed = False
            for line in lines[1:]:
                if line.strip() == _FRONTMATTER_DELIM:
                    closed = True
                    break
                body_lines.append(line)
            # When the closing fence is missing, fall back to the whole tail.
            fence_body = "\n".join(body_lines) if closed else "\n".join(lines[1:])
            return _safe_yaml_mapping(fence_body)
    # Case 2: raw YAML mapping (no fences).
    return _safe_yaml_mapping(stripped)


def _safe_yaml_mapping(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        loaded: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(loaded, dict):
        # YAML loads keys as Any; coerce to str-keyed for jsonschema.
        loaded_dict = cast("dict[Any, Any]", loaded)
        return {str(k): _normalize_yaml_value(v) for k, v in loaded_dict.items()}
    return None


def _normalize_yaml_value(value: Any) -> Any:
    """Coerce non-JSON YAML scalars into JSON-friendly equivalents.

    PyYAML returns ``datetime.date`` / ``datetime.datetime`` for ISO scalars,
    which ``jsonschema`` then rejects with ``"is not of type string"``. We
    normalise those to ISO 8601 strings so ``format: date`` / ``date-time``
    checks see what the author wrote on disk.
    """
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, list):
        items = cast("list[Any]", value)
        return [_normalize_yaml_value(item) for item in items]
    if isinstance(value, dict):
        nested = cast("dict[Any, Any]", value)
        return {str(k): _normalize_yaml_value(v) for k, v in nested.items()}
    return value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_format_checker_singleton: FormatChecker | None = None


def _format_checker() -> FormatChecker:
    """Return a shared FormatChecker that validates the ``date`` format strictly.

    The vanilla Draft-07 ``date`` check in ``jsonschema`` only fires when the
    optional ``strict-rfc3339`` / ``rfc3339-validator`` dependency is installed.
    To keep behaviour deterministic across environments we register our own
    ISO-8601 date predicate.
    """
    global _format_checker_singleton
    if _format_checker_singleton is None:
        fc = FormatChecker()

        @fc.checks("date", ValueError)
        def _check_date(value: Any) -> bool:
            if not isinstance(value, str):
                return False
            _dt.date.fromisoformat(value)
            return True

        # Reference the registered checker so static analysers do not flag it.
        assert _check_date is not None
        _format_checker_singleton = fc
    return _format_checker_singleton


def _issue_from_error(err: jsonschema.ValidationError) -> ValidationIssue:
    path_parts: tuple[str | int, ...] = tuple(err.absolute_path)
    code = err.validator if isinstance(err.validator, str) else ""
    return ValidationIssue(message=err.message, path=path_parts, code=code)


def _missing_recommended(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            message=f"recommended key missing: {key}",
            path=(key,),
            code="recommended_missing",
        )
        for key in keys
        if key not in metadata
    ]


def validate_ticket_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    schema_version: str = "v1",
    strict: bool = False,
    path: Path | None = None,
) -> ValidationReport:
    """Validate a parsed frontmatter mapping. Mainly for in-memory callers.

    With ``strict=True`` recommended-key warnings are promoted to errors.
    """
    target = path if path is not None else Path("<memory>")
    if metadata is None:
        return ValidationReport.error(target, "no frontmatter", code="no_frontmatter")
    # The runtime check is load-bearing: callers reach this branch from
    # JSON / YAML deserialisation paths that may yield a list or scalar.
    if not isinstance(metadata, Mapping):  # type: ignore[unreachable]
        return ValidationReport.error(target, "frontmatter is not a mapping", code="not_mapping")

    schema = load_schema(schema_version)
    validator: Any = jsonschema.Draft7Validator(schema, format_checker=_format_checker())
    instance: dict[str, Any] = dict(metadata)
    raw_errors = cast("list[jsonschema.ValidationError]", list(validator.iter_errors(instance)))
    errors = [_issue_from_error(e) for e in raw_errors]
    recommended = _missing_recommended(metadata, RECOMMENDED_KEYS)

    if strict:
        errors.extend(recommended)
        return ValidationReport(path=target, errors=errors, warnings=[])
    return ValidationReport(path=target, errors=errors, warnings=recommended)


def validate_ticket(
    path: Path | str,
    *,
    schema_version: str = "v1",
    strict: bool = False,
) -> ValidationReport:
    """Validate a single ticket file on disk.

    The file may be:

    - a markdown file with leading ``---`` fenced frontmatter;
    - a YAML file with the same ``---`` fences;
    - a YAML file containing a single mapping (no fences).

    On a parse error or a missing/empty file the returned report has
    ``ok=False`` and a single error.
    """
    target = Path(path)
    if not target.exists():
        return ValidationReport.error(target, "file does not exist", code="missing_file")
    if not target.is_file():
        return ValidationReport.error(target, "path is not a file", code="not_a_file")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationReport.error(target, f"could not read file: {exc}", code="read_error")
    metadata = parse_frontmatter(text)
    if metadata is None:
        return ValidationReport.error(target, "no frontmatter", code="no_frontmatter")
    report = validate_ticket_metadata(
        metadata,
        schema_version=schema_version,
        strict=strict,
        path=target,
    )
    return report
