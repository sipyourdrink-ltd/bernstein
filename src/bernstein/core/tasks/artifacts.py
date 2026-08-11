"""Artifact contract: kinds, canonical serialisation, and typed criteria (#2608).

Slice 1 of the generalised task/artifact contract. A non-coding agent produces
a *report*, a *dataset*, an *action log*, or an *ops result* instead of a code
diff. Each such artifact needs a single, byte-stable canonical form so its
``content_hash`` is a deterministic content-addressed identity: two operators
with equal inputs must produce byte-identical bytes, hence the same hash, hence
the same signed lineage entry.

Every canonicaliser routes through one shared core (stable key ordering, fixed
UTF-8, ``\\n`` newlines):

* text kinds (``code_diff`` / ``report``) normalise newlines to ``\\n`` and
  *reject* non-NFC input rather than repairing it - the same reject-don't-repair
  policy the other canonical cores in the codebase apply;
* JSONL kinds (``dataset`` / ``action_log``) emit one JCS-canonical JSON object
  per line, ``\\n``-separated;
* the JSON-object kind (``ops_result``) emits a single JCS-canonical object.

This module deliberately has no dependency on the task model or the lineage
store so it can be imported from both sides without a cycle.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import operator
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ArtifactKind(StrEnum):
    """Closed set of artifact kinds a task can declare it produces."""

    CODE_DIFF = "code_diff"
    REPORT = "report"
    DATASET = "dataset"
    ACTION_LOG = "action_log"
    OPS_RESULT = "ops_result"
    FINDING = "finding"


#: Kinds whose canonical form is normalised UTF-8 *text*.
_TEXT_KINDS: frozenset[ArtifactKind] = frozenset({ArtifactKind.CODE_DIFF, ArtifactKind.REPORT})
#: Kinds whose canonical form is JSONL - one JCS object per line, ``\n``-joined.
_JSONL_KINDS: frozenset[ArtifactKind] = frozenset({ArtifactKind.DATASET, ArtifactKind.ACTION_LOG})
#: Kinds whose canonical form is a single JCS-canonical JSON object.
_JSON_OBJECT_KINDS: frozenset[ArtifactKind] = frozenset({ArtifactKind.OPS_RESULT})

#: The three typed criteria that operate on artifact bytes (issue #2608).
ARTIFACT_CRITERION_TYPES: frozenset[str] = frozenset({"schema_valid", "criteria_match", "hash_stable"})

#: Closed set of predicate operators the ``criteria_match`` evaluator accepts.
_ALLOWED_OPS: frozenset[str] = frozenset({"exists", "eq", "ne", "contains", "gt", "ge", "lt", "le"})


class CanonicalisationError(ValueError):
    """Raised when an artifact cannot be canonicalised under its kind's rule."""


class ArtifactSpecError(ValueError):
    """Raised when an operator-declared artifact block is malformed (#3110).

    ``field`` names the offending key as a dotted path rooted at the
    declaration key (e.g. ``artifact_spec.kind``), so every loader points the
    operator at the exact field that was wrong. Fail-closed on purpose: a
    malformed declaration stops the load. It must never default to
    ``code_diff``, because a task that silently completes on a git SHA is the
    wrong completion identity for the artifact the operator asked for.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


# ---------------------------------------------------------------------------
# Shared canonical core
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: Any) -> bytes:
    """Shared JSON canonical core: sorted keys, minimal separators, UTF-8.

    ``allow_nan=False`` rejects NaN / Infinity, which have no canonical JSON
    form. Every JSON-shaped kind routes through this single serialiser so two
    kinds can never disagree on key ordering or separators.
    """
    try:
        return json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalisationError(f"value is not canonical-JSON serialisable: {exc}") from exc


def _normalise_newlines(text: str) -> str:
    """Fold CRLF and a lone CR to ``\\n``. Part of the shared text core."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _canonical_text_bytes(text: str) -> bytes:
    """Shared text canonical core: normalise newlines, require NFC, encode UTF-8.

    Non-NFC text is *rejected*, not repaired, so a caller can never silently
    ship two byte-different inputs that render the same. Newline normalisation
    runs first; it only touches ASCII control bytes and so never changes NFC
    status.
    """
    normalised = _normalise_newlines(text)
    if not unicodedata.is_normalized("NFC", normalised):
        raise CanonicalisationError("text artifact is not NFC-normalised (reject-don't-repair policy)")
    return normalised.encode("utf-8")


def _coerce_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalisationError(f"text artifact bytes are not valid UTF-8: {exc}") from exc
    raise CanonicalisationError(f"text artifact must be str or bytes, got {type(raw).__name__}")


def _coerce_rows(raw: Any) -> list[Any]:
    if isinstance(raw, (str, bytes, dict)):
        raise CanonicalisationError("JSONL artifact must be a sequence of JSON objects, not a scalar or mapping")
    try:
        return list(raw)
    except TypeError as exc:
        raise CanonicalisationError(f"JSONL artifact must be an iterable of rows: {exc}") from exc


# ---------------------------------------------------------------------------
# Per-kind canonicalisers + content hash
# ---------------------------------------------------------------------------


def _canonical_finding_bytes(raw: Any) -> bytes:
    """Canonicalise a SARIF 2.1.0 finding artifact for content-addressing.
    Projects the finding down to stable identity fields, deliberately dropping
    the raw line number so cosmetic shifts don't change the hash. Binds tool
    context so the identity is anchored to the exact invocation.
    """
    if not isinstance(raw, dict):
        raise CanonicalisationError(f"finding artifact must be a mapping, got {type(raw).__name__}")

    # Extract SARIF result fields
    rule_id = str(raw.get("ruleId", ""))

    # Normalise artifact location URI to forward slashes
    artifact_location = raw.get("artifactLocation", {})
    uri = str(artifact_location.get("uri", "")).replace("\\", "/")

    # Hash the snippet text instead of using the raw line number
    region = raw.get("region", {})
    snippet_text = str(region.get("snippet", {}).get("text", ""))
    snippet_hash = "sha256:" + hashlib.sha256(snippet_text.encode("utf-8")).hexdigest()

    # Bind context fields required by the issue
    projected = {
        "ruleId": rule_id,
        "uri": uri,
        "snippet_hash": snippet_hash,
        "tool": str(raw.get("tool", "")),
        "tool_version": str(raw.get("tool_version", "")),
        "pinned_digest": str(raw.get("pinned_digest", "")),
        "invocation_argv_hash": str(raw.get("invocation_argv_hash", "")),
        "target": str(raw.get("target", "")),
    }

    return _canonical_json_bytes(projected)


def canonicalise_artifact(kind: ArtifactKind | str, raw: Any) -> bytes:
    """Return the canonical bytes for ``raw`` under ``kind``'s rule.

    Raises :class:`CanonicalisationError` when the input does not fit the
    kind's shape or violates the reject-don't-repair policy.
    """
    k = ArtifactKind(kind)
    if k in _TEXT_KINDS:
        return _canonical_text_bytes(_coerce_text(raw))
    if k in _JSONL_KINDS:
        rows = _coerce_rows(raw)
        return b"\n".join(_canonical_json_bytes(row) for row in rows)
    if k in _JSON_OBJECT_KINDS:
        return _canonical_json_bytes(raw)
    if k is ArtifactKind.FINDING:
        return _canonical_finding_bytes(raw)
    raise CanonicalisationError(f"no canonicaliser registered for kind {k!r}")


def content_hash(canonical_bytes: bytes) -> str:
    """Return the ``sha256:`` content hash of already-canonical bytes."""
    return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


def artifact_content_hash(kind: ArtifactKind | str, raw: Any) -> str:
    """Canonicalise ``raw`` under ``kind`` and return its content hash."""
    return content_hash(canonicalise_artifact(kind, raw))


def _parse_json_document(kind: ArtifactKind, canonical_bytes: bytes) -> Any:
    """Parse canonical artifact bytes back into a JSON document for criteria.

    JSONL kinds parse to a ``list`` of row objects; the JSON-object kind parses
    to the object. Text kinds have no JSON document and raise.
    """
    if kind in _JSONL_KINDS:
        if not canonical_bytes:
            return []
        return [json.loads(line) for line in canonical_bytes.split(b"\n")]
    if kind in _JSON_OBJECT_KINDS:
        return json.loads(canonical_bytes)
    raise CanonicalisationError(f"kind {kind.value!r} has no JSON document form")


# ---------------------------------------------------------------------------
# Typed criterion evaluators (closed set, never execute artifact-supplied code)
# ---------------------------------------------------------------------------


def evaluate_criterion(
    criterion_type: str,
    criterion_value: str,
    *,
    artifact: Any,
    kind: ArtifactKind | str,
) -> tuple[bool, str]:
    """Evaluate one typed artifact criterion against ``artifact``.

    * ``hash_stable`` - re-canonicalise ``artifact`` and compare its content
      hash to ``criterion_value`` (an expected ``sha256:...`` string).
    * ``schema_valid`` - validate the artifact's JSON document against the JSON
      Schema in ``criterion_value``; JSONL kinds validate each row.
    * ``criteria_match`` - evaluate a closed predicate set (JSON in
      ``criterion_value``) over the artifact's JSON document.

    Returns ``(passed, detail)``. An unknown criterion type returns
    ``(False, ...)`` rather than raising so a caller can evaluate a mixed list
    without a guard at every call site.
    """
    k = ArtifactKind(kind)
    if criterion_type == "hash_stable":
        expected = criterion_value.strip()
        try:
            actual = artifact_content_hash(k, artifact)
        except CanonicalisationError as exc:
            return False, f"artifact does not canonicalise: {exc}"
        ok = _hmac.compare_digest(actual, expected)
        return ok, ("hash stable" if ok else f"hash drift: expected {expected}, got {actual}")
    if criterion_type == "schema_valid":
        return _eval_schema_valid(k, artifact, criterion_value)
    if criterion_type == "criteria_match":
        return _eval_criteria_match(k, artifact, criterion_value)
    return False, f"not an artifact criterion type: {criterion_type!r}"


def _json_document_for(kind: ArtifactKind, artifact: Any) -> Any:
    """Canonicalise ``artifact`` under ``kind`` and parse its JSON document.

    Raises :class:`CanonicalisationError` for text kinds (no JSON document) or
    when the artifact does not canonicalise under the kind's rule.
    """
    canonical = canonicalise_artifact(kind, artifact)
    return _parse_json_document(kind, canonical)


def _eval_schema_valid(kind: ArtifactKind, artifact: Any, schema_text: str) -> tuple[bool, str]:
    import jsonschema

    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as exc:
        return False, f"schema is not valid JSON: {exc}"
    try:
        doc = _json_document_for(kind, artifact)
    except CanonicalisationError as exc:
        return False, f"artifact has no JSON document to validate: {exc}"
    try:
        validator = jsonschema.Draft202012Validator(schema)
    except jsonschema.exceptions.SchemaError as exc:
        return False, f"schema is not a valid JSON Schema: {exc.message}"
    if kind in _JSONL_KINDS:
        for i, row in enumerate(doc):
            errors = sorted(validator.iter_errors(row), key=lambda e: list(e.path))
            if errors:
                return False, f"row {i} fails schema: {errors[0].message}"
        return True, f"all {len(doc)} row(s) valid"
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        return False, f"artifact fails schema: {errors[0].message}"
    return True, "schema valid"


def _eval_criteria_match(kind: ArtifactKind, artifact: Any, preds_text: str) -> tuple[bool, str]:
    try:
        preds = json.loads(preds_text)
    except json.JSONDecodeError as exc:
        return False, f"criteria set is not valid JSON: {exc}"
    if not isinstance(preds, list):
        return False, "criteria set must be a JSON list of predicates"
    try:
        doc = _json_document_for(kind, artifact)
    except CanonicalisationError as exc:
        return False, f"artifact has no JSON document to match: {exc}"
    for i, pred in enumerate(preds):
        if not isinstance(pred, dict):
            return False, f"predicate {i} must be a mapping"
        op = pred.get("op")
        if op not in _ALLOWED_OPS:
            return False, f"predicate {i} has unknown op {op!r}"
        path = str(pred.get("path", ""))
        found, actual = _resolve_path(doc, path)
        ok, detail = _apply_op(str(op), found, actual, pred.get("value"))
        if not ok:
            return False, f"predicate {i} ({op} {path!r}) failed: {detail}"
    return True, f"all {len(preds)} predicate(s) matched"


def _resolve_path(doc: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path (dict keys, list indices) into ``doc``.

    Returns ``(found, value)``; ``found`` is ``False`` when any segment does
    not resolve. An empty path resolves to the document itself.
    """
    if path == "":
        return True, doc
    cur = doc
    for seg in path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return False, None
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                idx = int(seg)
            except ValueError:
                return False, None
            if idx < 0 or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            return False, None
    return True, cur


def _apply_op(op: str, found: bool, actual: Any, expected: Any) -> tuple[bool, str]:
    if op == "exists":
        want = True if expected is None else bool(expected)
        return (found == want), f"exists={found}"
    if not found:
        return False, "path not found"
    if op == "eq":
        return actual == expected, f"{actual!r} == {expected!r}"
    if op == "ne":
        return actual != expected, f"{actual!r} != {expected!r}"
    if op == "contains":
        try:
            return (expected in actual), f"{expected!r} in {actual!r}"
        except TypeError:
            return False, "value is not a container"
    # Numeric comparisons; reject bool (a bool is an int subclass) and non-numbers.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False, "numeric comparison on a boolean"
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False, "numeric comparison on a non-number"
    cmp = {"gt": operator.gt, "ge": operator.ge, "lt": operator.lt, "le": operator.le}[op]
    return cmp(actual, expected), f"{actual!r} {op} {expected!r}"


# ---------------------------------------------------------------------------
# Typed spec dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactCriterion:
    """One typed verification criterion evaluated against artifact bytes.

    Mirrors the ``{type, value}`` shape of :class:`CompletionSignal` but is
    closed to the three artifact criterion types so an :class:`ArtifactSpec`
    never carries a filesystem-oriented signal.
    """

    type: Literal["schema_valid", "criteria_match", "hash_stable"]
    value: str

    def __post_init__(self) -> None:
        if self.type not in ARTIFACT_CRITERION_TYPES:
            raise ValueError(f"unknown artifact criterion type: {self.type!r}")

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactCriterion:
        return cls(type=str(data["type"]), value=str(data["value"]))  # type: ignore[arg-type]

    def evaluate(self, *, artifact: Any, kind: ArtifactKind | str) -> tuple[bool, str]:
        """Evaluate this criterion against ``artifact`` under ``kind``."""
        return evaluate_criterion(self.type, self.value, artifact=artifact, kind=kind)


@dataclass(frozen=True)
class ArtifactSpec:
    """Declared artifact contract for a task: kind, canonicalisation, criteria.

    Defaults to ``code_diff`` so an existing coding task that carries no spec is
    unchanged. ``canonicalisation`` names the serialisation rule; an empty
    string means "the kind's default rule" (see :attr:`canonical_rule`).

    ``output_path`` is the workdir-relative POSIX path the agent writes its
    produced artifact to. It is what makes an artifact-mode task *executable*:
    the completion path reads those bytes, canonicalises them under
    :attr:`kind`, and records the signed lineage entry that stands in for the
    git SHA a coding task would have produced. An empty string selects the
    per-task default (see
    :func:`bernstein.core.tasks.artifact_completion.artifact_output_path`).
    """

    kind: ArtifactKind = ArtifactKind.CODE_DIFF
    canonicalisation: str = ""
    criteria: tuple[ArtifactCriterion, ...] = field(default_factory=tuple)
    output_path: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            object.__setattr__(self, "kind", ArtifactKind(self.kind))
        if not isinstance(self.criteria, tuple):
            object.__setattr__(self, "criteria", tuple(self.criteria))

    @property
    def canonical_rule(self) -> str:
        """The effective canonicalisation rule id (falls back to the kind)."""
        return self.canonicalisation or self.kind.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "canonicalisation": self.canonicalisation,
            "criteria": [c.to_dict() for c in self.criteria],
            "output_path": self.output_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactSpec:
        criteria = tuple(ArtifactCriterion.from_dict(c) for c in data.get("criteria", []) if isinstance(c, dict))
        return cls(
            kind=ArtifactKind(str(data.get("kind", ArtifactKind.CODE_DIFF.value))),
            canonicalisation=str(data.get("canonicalisation", "")),
            criteria=criteria,
            output_path=str(data.get("output_path", "")),
        )

    @classmethod
    def default(cls) -> ArtifactSpec:
        """Return the default ``code_diff`` spec (the coding-task contract)."""
        return cls()


# ---------------------------------------------------------------------------
# The strict declaration parser shared by every operator surface (#3110)
# ---------------------------------------------------------------------------

#: YAML / payload key an operator declares the artifact contract under, and
#: the root of every :class:`ArtifactSpecError` field path.
ARTIFACT_SPEC_KEY = "artifact_spec"

_ALLOWED_SPEC_KEYS: frozenset[str] = frozenset({"kind", "canonicalisation", "criteria", "output_path"})
_ALLOWED_CRITERION_KEYS: frozenset[str] = frozenset({"type", "value"})


def validate_artifact_output_path(declared: str, *, field: str = f"{ARTIFACT_SPEC_KEY}.output_path") -> str:
    """Validate a declared artifact output path and return its POSIX form.

    The path must stay workdir-relative: absolute paths, drive-letter paths,
    and any ``..`` traversal are refused *at declaration time*, before a task
    exists and before any bytes are read. The same rules gate the completion
    path (:func:`bernstein.core.tasks.artifact_completion.artifact_output_path`),
    so a declaration that loads is one the completion path will accept.

    Raises:
        ArtifactSpecError: The path is absolute or escapes the workdir.
    """
    normalised = declared.replace("\\", "/")
    if normalised.startswith("/") or (len(normalised) > 2 and normalised[1:3] == ":/"):
        raise ArtifactSpecError(field, f"must be workdir-relative, got {declared!r}")
    if any(seg == ".." for seg in normalised.split("/")):
        raise ArtifactSpecError(field, f"must not traverse out of the workdir: {declared!r}")
    return normalised


def _parse_declared_criteria(raw: Any, *, root: str) -> tuple[ArtifactCriterion, ...]:
    """Parse the ``criteria`` list of a declaration. Strict; see the parser."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ArtifactSpecError(
            f"{root}.criteria", f"must be a list of {{type, value}} mappings, got {type(raw).__name__}"
        )
    parsed: list[ArtifactCriterion] = []
    for i, entry in enumerate(raw):
        path = f"{root}.criteria[{i}]"
        if not isinstance(entry, dict):
            raise ArtifactSpecError(path, f"must be a mapping with 'type' and 'value', got {type(entry).__name__}")
        unknown = sorted(set(map(str, entry)) - _ALLOWED_CRITERION_KEYS)
        if unknown:
            raise ArtifactSpecError(f"{path}.{unknown[0]}", "unknown key (allowed keys: type, value)")
        ctype = entry.get("type")
        if not isinstance(ctype, str) or ctype not in ARTIFACT_CRITERION_TYPES:
            allowed = ", ".join(sorted(ARTIFACT_CRITERION_TYPES))
            raise ArtifactSpecError(f"{path}.type", f"must be one of: {allowed}; got {ctype!r}")
        value = entry.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ArtifactSpecError(f"{path}.value", "must be a non-empty string")
        parsed.append(ArtifactCriterion(type=ctype, value=value))  # type: ignore[arg-type]
    return tuple(parsed)


def parse_artifact_spec(raw: object) -> ArtifactSpec:
    """Parse an operator-declared ``artifact_spec`` block into an :class:`ArtifactSpec`.

    The one strict parser behind every declaration surface - the plan schema
    and loader, the backlog frontmatter, the CLI flags, and the task server's
    create boundary - so the surfaces cannot drift (issue #3110).

    Fail-closed by design. Anything malformed raises
    :class:`ArtifactSpecError` naming the offending field: an unknown kind, a
    missing or unsafe ``output_path``, an unknown key, a malformed criterion.
    Unknown keys are refused rather than ignored, because a typo'd key that is
    dropped silently turns a declared artifact contract into a default coding
    task - the exact defect this parser exists to close.

    Rules:

    * ``kind`` is required and must be a member of :class:`ArtifactKind`.
    * an artifact kind (anything but ``code_diff``) requires a non-empty,
      workdir-relative ``output_path`` - the declaration says *where* the
      deliverable lands, explicitly;
    * ``kind: code_diff`` is accepted bare (it restates the default coding
      contract) but takes no ``output_path`` and no ``criteria``;
    * ``canonicalisation`` may only name the kind's own default rule (or be
      omitted / empty) - no alternative rule ships, and accepting an unknown
      rule name would be a claim the completion path cannot honour;
    * each criterion is exactly ``{type, value}`` with a type from
      :data:`ARTIFACT_CRITERION_TYPES` and a non-empty string value.
    """
    root = ARTIFACT_SPEC_KEY
    if not isinstance(raw, dict):
        raise ArtifactSpecError(root, f"must be a mapping with a 'kind' field, got {type(raw).__name__}")
    unknown = sorted(set(map(str, raw)) - _ALLOWED_SPEC_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_SPEC_KEYS))
        raise ArtifactSpecError(f"{root}.{unknown[0]}", f"unknown key (allowed keys: {allowed})")

    kind_values = ", ".join(k.value for k in ArtifactKind)
    if "kind" not in raw:
        raise ArtifactSpecError(f"{root}.kind", f"is required (one of: {kind_values})")
    kind_raw = raw["kind"]
    if not isinstance(kind_raw, str):
        raise ArtifactSpecError(f"{root}.kind", f"must be a string, got {type(kind_raw).__name__}")
    try:
        kind = ArtifactKind(kind_raw)
    except ValueError:
        raise ArtifactSpecError(f"{root}.kind", f"unknown artifact kind {kind_raw!r} (one of: {kind_values})") from None

    output_raw = raw.get("output_path") or ""
    if not isinstance(output_raw, str):
        raise ArtifactSpecError(f"{root}.output_path", f"must be a string, got {type(output_raw).__name__}")
    output_path = output_raw.strip()

    if kind is ArtifactKind.CODE_DIFF:
        if output_path:
            raise ArtifactSpecError(
                f"{root}.output_path", "code_diff tasks complete on the git path and take no output_path"
            )
        if raw.get("criteria"):
            raise ArtifactSpecError(
                f"{root}.criteria", "code_diff tasks complete on the git path and take no artifact criteria"
            )
    else:
        if not output_path:
            raise ArtifactSpecError(
                f"{root}.output_path",
                f"is required for kind {kind.value!r}: the workdir-relative path the agent writes the artifact to",
            )
        output_path = validate_artifact_output_path(output_path)

    canon_raw = raw.get("canonicalisation") or ""
    if not isinstance(canon_raw, str):
        raise ArtifactSpecError(f"{root}.canonicalisation", f"must be a string, got {type(canon_raw).__name__}")
    canonicalisation = canon_raw.strip()
    if canonicalisation and canonicalisation != kind.value:
        raise ArtifactSpecError(
            f"{root}.canonicalisation",
            f"unknown rule {canonicalisation!r}; the only rule shipped for kind {kind.value!r} is its default"
            " (omit the key or repeat the kind)",
        )

    criteria = _parse_declared_criteria(raw.get("criteria"), root=root)
    return ArtifactSpec(kind=kind, canonicalisation=canonicalisation, criteria=criteria, output_path=output_path)


__all__ = [
    "ARTIFACT_CRITERION_TYPES",
    "ARTIFACT_SPEC_KEY",
    "ArtifactCriterion",
    "ArtifactKind",
    "ArtifactSpec",
    "ArtifactSpecError",
    "CanonicalisationError",
    "artifact_content_hash",
    "canonicalise_artifact",
    "content_hash",
    "evaluate_criterion",
    "parse_artifact_spec",
    "validate_artifact_output_path",
]
