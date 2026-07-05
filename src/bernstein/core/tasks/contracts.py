"""Schema-enforced worker terminal payloads (#2244).

Worker completion payloads used to be free-form prose in
``result_summary``; a worker that could not proceed had no structured way
to say so. This module defines the closed contract both terminal shapes
must satisfy at the completion API boundary:

* :class:`WorkerCompletion` - a successful terminal payload (summary,
  files changed, optional verification evidence, optional receipt ref).
* :class:`WorkerRefusal` - a typed refusal with a closed
  :class:`RefusalKind` taxonomy and per-kind machine-readable fields.
* :class:`ContractViolation` - raised when a payload fails validation;
  carries the JSONPath-style ``path`` of the offending field so the
  failure is diagnosable from the ledger alone.
* :func:`derive_follow_up_specs` - deterministic derivation of follow-up
  task specs from a ``scope_exceeded`` refusal. The same payload always
  produces the same follow-up task ids, so a re-delivered refusal cannot
  duplicate the split.

Validation is strict: unknown fields, unknown refusal kinds, and
malformed JSON are contract violations, never silent accepts. The
contract is versioned via :data:`WORKER_CONTRACT_VERSION`; the version
and validation outcome are recorded in the task's ledger/audit trail by
the task store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

#: Version tag for the worker terminal-payload contract. Recorded in the
#: task ledger and the HMAC-chained audit log for every validated payload.
WORKER_CONTRACT_VERSION = "worker-completion/v1"

#: Hard cap for the completion summary, in characters.
SUMMARY_MAX_CHARS = 2000


class RefusalKind(StrEnum):
    """Closed taxonomy of worker refusal outcomes.

    An unknown kind is a contract violation - there is deliberately no
    catch-all member, so the orchestrator can route every refusal
    deterministically.
    """

    AWAITING_OPERATOR = "awaiting_operator"
    SCOPE_EXCEEDED = "scope_exceeded"
    UNDERSPECIFIED = "underspecified"
    BLOCKED_ON_DEPENDENCY = "blocked_on_dependency"


class ContractViolation(Exception):
    """A terminal payload failed schema validation.

    Attributes:
        path: JSONPath-style locator of the offending field
            (e.g. ``$.summary``, ``$.proposed_split[1]``).
        message: Human-readable description of the violation.
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class VerificationEvidence:
    """Verification step the worker ran before completing."""

    command: str
    exit_code: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {"command": self.command, "exit_code": self.exit_code}


@dataclass(frozen=True)
class WorkerCompletion:
    """Validated successful terminal payload."""

    summary: str
    files_changed: tuple[str, ...] = ()
    verification: VerificationEvidence | None = None
    receipt_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation including the contract tag."""
        data: dict[str, Any] = {
            "contract": WORKER_CONTRACT_VERSION,
            "summary": self.summary,
            "files_changed": list(self.files_changed),
        }
        if self.verification is not None:
            data["verification"] = self.verification.to_dict()
        if self.receipt_ref is not None:
            data["receipt_ref"] = self.receipt_ref
        return data


@dataclass(frozen=True)
class WorkerRefusal:
    """Validated refusal terminal payload.

    Per-kind payload fields:
        * ``scope_exceeded`` - ``proposed_split`` (non-empty list).
        * ``underspecified`` / ``awaiting_operator`` - ``question``.
        * ``blocked_on_dependency`` - ``blocking_dep``.
    """

    kind: RefusalKind
    detail: str
    question: str = ""
    proposed_split: tuple[str, ...] = ()
    blocking_dep: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation including the contract tag."""
        data: dict[str, Any] = {
            "contract": WORKER_CONTRACT_VERSION,
            "kind": self.kind.value,
            "detail": self.detail,
        }
        if self.kind is RefusalKind.SCOPE_EXCEEDED:
            data["proposed_split"] = list(self.proposed_split)
        elif self.kind in (RefusalKind.UNDERSPECIFIED, RefusalKind.AWAITING_OPERATOR):
            data["question"] = self.question
        elif self.kind is RefusalKind.BLOCKED_ON_DEPENDENCY:
            data["blocking_dep"] = self.blocking_dep
        return data


@dataclass(frozen=True)
class FollowUpSpec:
    """Deterministic follow-up task spec derived from a refusal split."""

    task_id: str
    title: str
    description: str


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_str(payload: dict[str, Any], key: str, *, max_chars: int | None = None) -> str:
    """Return a required non-empty string field or raise ContractViolation."""
    if key not in payload:
        raise ContractViolation(f"$.{key}", "required field is missing")
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(f"$.{key}", "must be a non-empty string")
    if max_chars is not None and len(value) > max_chars:
        raise ContractViolation(f"$.{key}", f"exceeds the {max_chars}-character cap")
    return value


def _check_contract_tag(payload: dict[str, Any]) -> None:
    """Reject a payload that claims a different contract version."""
    if "contract" in payload and payload["contract"] != WORKER_CONTRACT_VERSION:
        raise ContractViolation("$.contract", f"unsupported contract version (expected {WORKER_CONTRACT_VERSION})")


def _check_allowed_keys(payload: dict[str, Any], allowed: frozenset[str]) -> None:
    """Reject unknown top-level fields (closed schema)."""
    for key in payload:
        if key not in allowed:
            raise ContractViolation(f"$.{key}", "unknown field")


def _parse_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    """Validate an optional list-of-non-empty-strings field."""
    raw = payload.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContractViolation(f"$.{key}", "must be a list of strings")
    items = cast("list[object]", raw)
    result: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise ContractViolation(f"$.{key}[{i}]", "must be a non-empty string")
        result.append(item)
    return tuple(result)


def _parse_verification(payload: dict[str, Any]) -> VerificationEvidence | None:
    """Validate the optional ``verification`` sub-object."""
    raw = payload.get("verification")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ContractViolation("$.verification", "must be an object or null")
    obj = cast("dict[str, Any]", raw)
    for key in obj:
        if key not in ("command", "exit_code"):
            raise ContractViolation(f"$.verification.{key}", "unknown field")
    command = obj.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ContractViolation("$.verification.command", "must be a non-empty string")
    exit_code = obj.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ContractViolation("$.verification.exit_code", "must be an integer")
    return VerificationEvidence(command=command, exit_code=exit_code)


_COMPLETION_KEYS = frozenset({"contract", "summary", "files_changed", "verification", "receipt_ref"})
_REFUSAL_BASE_KEYS = frozenset({"contract", "kind", "detail"})
_REFUSAL_KIND_KEYS: dict[RefusalKind, frozenset[str]] = {
    RefusalKind.SCOPE_EXCEEDED: frozenset({"proposed_split"}),
    RefusalKind.UNDERSPECIFIED: frozenset({"question"}),
    RefusalKind.AWAITING_OPERATOR: frozenset({"question"}),
    RefusalKind.BLOCKED_ON_DEPENDENCY: frozenset({"blocking_dep"}),
}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_completion(payload: dict[str, Any]) -> WorkerCompletion:
    """Validate a completion-shaped payload.

    Args:
        payload: JSON-decoded object claiming the completion shape.

    Returns:
        The validated :class:`WorkerCompletion`.

    Raises:
        ContractViolation: On any schema mismatch, carrying the field path.
    """
    _check_contract_tag(payload)
    _check_allowed_keys(payload, _COMPLETION_KEYS)
    summary = _require_str(payload, "summary", max_chars=SUMMARY_MAX_CHARS)
    files_changed = _parse_string_list(payload, "files_changed")
    verification = _parse_verification(payload)
    receipt_ref_raw = payload.get("receipt_ref")
    receipt_ref: str | None = None
    if receipt_ref_raw is not None:
        if not isinstance(receipt_ref_raw, str) or not receipt_ref_raw.strip():
            raise ContractViolation("$.receipt_ref", "must be a non-empty string or null")
        receipt_ref = receipt_ref_raw
    return WorkerCompletion(
        summary=summary,
        files_changed=files_changed,
        verification=verification,
        receipt_ref=receipt_ref,
    )


def parse_refusal(payload: dict[str, Any]) -> WorkerRefusal:
    """Validate a refusal-shaped payload (closed kind enum).

    Args:
        payload: JSON-decoded object carrying a ``kind`` field.

    Returns:
        The validated :class:`WorkerRefusal`.

    Raises:
        ContractViolation: On unknown kinds, missing per-kind fields, or
            any other schema mismatch.
    """
    _check_contract_tag(payload)
    kind_raw = payload.get("kind")
    if not isinstance(kind_raw, str):
        raise ContractViolation("$.kind", "must be a string refusal kind")
    try:
        kind = RefusalKind(kind_raw)
    except ValueError:
        allowed = ", ".join(k.value for k in RefusalKind)
        raise ContractViolation("$.kind", f"unknown refusal kind (allowed: {allowed})") from None
    _check_allowed_keys(payload, _REFUSAL_BASE_KEYS | _REFUSAL_KIND_KEYS[kind])
    detail = _require_str(payload, "detail", max_chars=SUMMARY_MAX_CHARS)

    if kind is RefusalKind.SCOPE_EXCEEDED:
        if "proposed_split" not in payload:
            raise ContractViolation("$.proposed_split", "required for scope_exceeded")
        split = _parse_string_list(payload, "proposed_split")
        if not split:
            raise ContractViolation("$.proposed_split", "must contain at least one item")
        return WorkerRefusal(kind=kind, detail=detail, proposed_split=split)
    if kind in (RefusalKind.UNDERSPECIFIED, RefusalKind.AWAITING_OPERATOR):
        question = _require_str(payload, "question")
        return WorkerRefusal(kind=kind, detail=detail, question=question)
    # BLOCKED_ON_DEPENDENCY
    blocking_dep = _require_str(payload, "blocking_dep")
    return WorkerRefusal(kind=kind, detail=detail, blocking_dep=blocking_dep)


def parse_terminal_payload(data: object) -> WorkerCompletion | WorkerRefusal:
    """Validate a terminal payload, dispatching on the ``kind`` field.

    Args:
        data: JSON-decoded value. Objects with a ``kind`` key are parsed
            as refusals; all other objects as completions.

    Returns:
        A validated :class:`WorkerCompletion` or :class:`WorkerRefusal`.

    Raises:
        ContractViolation: When ``data`` is not an object or fails the
            shape-specific schema.
    """
    if not isinstance(data, dict):
        raise ContractViolation("$", "terminal payload must be a JSON object")
    payload = cast("dict[str, Any]", data)
    if "kind" in payload:
        return parse_refusal(payload)
    return parse_completion(payload)


def parse_terminal_payload_text(raw: str) -> WorkerCompletion | WorkerRefusal:
    """Parse a terminal payload delivered as a JSON string.

    Args:
        raw: The raw text (e.g. a ``result_summary`` that carries inline
            JSON instead of prose).

    Returns:
        The validated payload.

    Raises:
        ContractViolation: When the text is not valid JSON (path ``$``)
            or the decoded object fails validation.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractViolation("$", f"malformed JSON: {exc.msg} at char {exc.pos}") from exc
    return parse_terminal_payload(decoded)


def looks_like_contract_payload(text: str) -> bool:
    """Return True when *text* appears to be an inline JSON contract payload.

    Used at the API boundary to distinguish a structured payload embedded
    in ``result_summary`` from legacy prose summaries, which continue to
    be accepted unchanged. Anything that opens as a JSON object is treated
    as an attempted contract payload, so a truncated JSON document is a
    contract violation rather than a silently accepted prose summary.
    """
    return text.strip().startswith("{")


# ---------------------------------------------------------------------------
# Deterministic follow-up derivation (scope_exceeded routing)
# ---------------------------------------------------------------------------


def derive_follow_up_specs(parent_task_id: str, refusal: WorkerRefusal) -> list[FollowUpSpec]:
    """Derive the follow-up task set for a ``scope_exceeded`` refusal.

    The derivation is a pure function of ``(parent_task_id, contract
    version, split index, split text)``: task ids are content-addressed
    SHA-256 prefixes, so the same refusal payload always produces the
    same follow-up set and a re-delivered refusal cannot duplicate tasks.

    Args:
        parent_task_id: Id of the refused task.
        refusal: A validated refusal. Non-``scope_exceeded`` kinds yield
            an empty list.

    Returns:
        Ordered follow-up specs, one per ``proposed_split`` item.
    """
    if refusal.kind is not RefusalKind.SCOPE_EXCEEDED:
        return []
    specs: list[FollowUpSpec] = []
    for index, item in enumerate(refusal.proposed_split):
        seed = f"{parent_task_id}|{WORKER_CONTRACT_VERSION}|{index}|{item}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        title = item if len(item) <= 80 else item[:77] + "..."
        specs.append(
            FollowUpSpec(
                task_id=f"fu-{digest}",
                title=title,
                description=(f"{item}\n\nSplit from task {parent_task_id} after a scope_exceeded refusal."),
            )
        )
    return specs
