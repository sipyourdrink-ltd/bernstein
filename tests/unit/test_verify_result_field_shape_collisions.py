"""One field shape must mean one type.

A verification result is the most-copied shape in this repository. Eight
separate classes declared exactly ``(ok, reason, receipt)`` under eight names -
they are now specialisations of one :class:`~bernstein.core.verify_result.VerifyResult`
- and five more still declare ``(errors, ok)``, four declare
``(detail, name, passed)``. Every one of them means the same thing - did the
check pass, and why - but no two are related, so a caller that wants to handle
"any verification result" either hand-writes an adapter per class or, far more
often, is hardcoded to exactly one of them.

The duplicates were never decided on; they accumulated one module at a time,
each author reasonably declaring a small local result type rather than hunting
for an existing one. Nothing in the tree pushed back, so the count only ever
went up.

This test is that push-back. It collects every dataclass and ``BaseModel``
under ``src/bernstein`` whose name marks it as a check result, keys them by the
frozenset of their annotated field names, and fails when one key maps to two or
more declaration sites that are not on an allowlist below. The allowlist starts
populated with the collisions that exist today, so the test is green now and
the next colliding class has to either reuse the canonical type or argue for
itself in writing.

The allowlist is also checked in the other direction: an entry that no longer
matches a real collision fails the test. That is what makes the count a ratchet
- a migration must delete its entry, and the entry cannot outlive the debt it
describes.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Final, get_origin

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bernstein"

#: Name endings that mark a class as the outcome of a check.
#:
#: Deliberately narrow. This test is about verification results, not about
#: every dataclass in the tree - a `Config` and a `Request` that happen to
#: share three field names are not the same thing and never were.
_RESULT_SUFFIXES: Final = ("Result", "Verification", "Verdict", "Check", "Finding")

#: Trailing token that marks a wire DTO. ``SBOMVulnFindingResponse`` is the
#: HTTP shape of ``SBOMVulnFinding``, so it is in scope: the whole point of the
#: allowlist is to record that this particular pair is split on purpose.
_DTO_SUFFIX: Final = "Response"


@dataclass(frozen=True, slots=True)
class _Declaration:
    """One class declaration and where it was found."""

    module: str
    lineno: int
    name: str

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.name}"


@dataclass(frozen=True, slots=True)
class _AllowedShape:
    """A field shape that is permitted to be declared more than once."""

    fields: frozenset[str]
    classes: frozenset[str]
    reason: str


#: Collisions that are correct and must stay.
_DELIBERATE_SHAPES: Final[tuple[_AllowedShape, ...]] = (
    _AllowedShape(
        fields=frozenset(
            {
                "component_name",
                "component_version",
                "fix_version",
                "scanner",
                "severity",
                "summary",
                "vuln_id",
            }
        ),
        classes=frozenset({"SBOMVulnFinding", "SBOMVulnFindingResponse"}),
        reason=(
            "core/security/sbom.py declares the internal scan-result type; "
            "core/routes/sbom.py declares its HTTP response DTO. One is a wire "
            "contract that changes only with an API version, the other is free "
            "to change with the scanner. Collapsing them would make every "
            "internal field rename a breaking API change."
        ),
    ),
)

#: Collisions that exist today and are not yet resolved. Each entry names the
#: work that removes it. Adding to this tuple is not a fix.
_PENDING_SHAPES: Final[tuple[_AllowedShape, ...]] = (
    _AllowedShape(
        fields=frozenset({"action", "branch", "files_changed", "reason"}),
        classes=frozenset({"MergeResult"}),
        reason=(
            "core/orchestration/drain.py and core/orchestration/drain_merge.py "
            "declare the same name for the same shape. Not a verification "
            "result; consolidating it belongs with the drain modules, not here."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"attestation", "ok", "reason"}),
        classes=frozenset({"CleanRunVerifyResult", "EquivalenceVerifyResult"}),
        reason=(
            "eval/clean_run.py declares both, and they carry different "
            "attestation types. They are the `(ok, reason, X)` shape with the "
            "receipt slot renamed; folding them onto VerifyResult needs the "
            "attestation types reconciled first."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"catalog", "from_cache", "revalidated", "source_url"}),
        classes=frozenset({"FetchResult"}),
        reason=(
            "core/protocols/mcp_catalog/fetcher.py and core/skills/catalog/"
            "fetcher.py fetch different catalogs over the same HTTP caching "
            "protocol. Not a verification result."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"commit_sha", "cost_usd", "message", "success"}),
        classes=frozenset({"DispatchResult", "GroundedDispatchResult"}),
        reason=(
            "core/autofix/dispatcher.py and core/autofix/telemetry_grounded.py "
            "report an autofix dispatch, not a verification. Not a "
            "verification result."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"count", "errors", "status"}),
        classes=frozenset({"SpineVerifyResult", "MemoryVerifyResult"}),
        reason=(
            "core/lineage/spine.py and core/memory/chain.py verify two chains "
            "and report a count of entries walked. Pending: a `VerifyResult` "
            "specialisation once the errors/status pair is reconciled with "
            "the ok/reason pair the eight-way group uses."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"detail", "name", "passed"}),
        classes=frozenset({"GateResult", "ConformanceCheck", "ValidatorVerdict"}),
        reason=(
            "core/planning/run_summary.py, core/integrations/pr_gen.py, "
            "core/interop/a2a_conformance.py and core/tokens/"
            "compaction_validate.py declare one per-item check outcome under "
            "four names. Pending consolidation."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"entries", "errors", "head_hash", "ok"}),
        classes=frozenset({"AdmissionVerification", "LedgerVerification"}),
        reason=(
            "core/admission/verify.py and core/persistence/work_ledger.py "
            "verify a hash-chained journal and report its head. Pending "
            "consolidation with the other `(errors, ok, ...)` verifications."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"errors", "ok"}),
        classes=frozenset({"ReceiptVerification", "ManifestVerification", "ProjectionVerification"}),
        reason=(
            "core/orchestration/sla_receipt.py, core/orchestration/"
            "supervisor_receipt.py and core/sandbox/selection_receipt.py each "
            "declare their own `ReceiptVerification`; core/lineage/c2pa.py and "
            "core/observability/otel_projection.py declare the same shape "
            "under two more names. Consolidating the receipt-side ones is part "
            "of the receipt-protocol work, which owns the receipt fields."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"errors", "ok", "verified_key_id"}),
        classes=frozenset({"A2AReceiptVerification", "HeadSignatureVerification"}),
        reason=(
            "core/protocols/a2a/receipt.py and core/security/"
            "audit_head_signature.py verify a signature and report which key "
            "verified it. Pending consolidation."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"failures", "ok"}),
        classes=frozenset({"PolicyVerdict", "GateResult"}),
        reason=(
            "core/interop/a2a_consume.py and core/lineage/gate.py report a "
            "list of failures behind a boolean. Pending consolidation."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"ok", "reason", "receipt", "status"}),
        classes=frozenset({"RunGraphVerifyResult", "LadderVerifyResult"}),
        reason=(
            "core/lineage/run_graph.py and core/quality/verifier_ladder.py are "
            "the eight-way `(ok, reason, receipt)` shape plus a status field. "
            "They become `VerifyResult` subclasses that add `status` once the "
            "two status vocabularies are reconciled."
        ),
    ),
    _AllowedShape(
        fields=frozenset({"passed", "task_id", "violations"}),
        classes=frozenset({"PolicyCheckResult", "RuleEnforcerResult"}),
        reason=(
            "core/security/policy_engine.py and core/security/rule_enforcer.py "
            "report per-task policy violations. Pending consolidation."
        ),
    ),
)

_ALLOWED_SHAPES: Final[tuple[_AllowedShape, ...]] = _DELIBERATE_SHAPES + _PENDING_SHAPES


def _is_result_name(name: str) -> bool:
    """Return whether *name* reads as the outcome of a check."""
    stem = name.removesuffix(_DTO_SUFFIX)
    return stem.endswith(_RESULT_SUFFIXES)


def _base_names(node: ast.ClassDef) -> set[str]:
    """Return the bare names of *node*'s bases, ignoring their qualifiers."""
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _is_dataclass(node: ast.ClassDef) -> bool:
    """Return whether *node* carries a ``@dataclass`` decorator."""
    for decorator in node.decorator_list:
        applied = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = (
            applied.id
            if isinstance(applied, ast.Name)
            else applied.attr
            if isinstance(applied, ast.Attribute)
            else None
        )
        if name == "dataclass":
            return True
    return False


def _annotated_fields(node: ast.ClassDef) -> frozenset[str]:
    """Return the names of *node*'s own annotated fields."""
    return frozenset(
        stmt.target.id for stmt in node.body if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    )


def _shapes_by_fields() -> dict[frozenset[str], list[_Declaration]]:
    """Return every result-shaped declaration keyed by its field-name set."""
    shapes: dict[frozenset[str], list[_Declaration]] = defaultdict(list)
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a parse failure is another test's problem
            continue
        module = path.relative_to(SOURCE_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_result_name(node.name):
                continue
            if not (_is_dataclass(node) or "BaseModel" in _base_names(node)):
                continue
            fields = _annotated_fields(node)
            if not fields:
                continue
            shapes[fields].append(_Declaration(module=module, lineno=node.lineno, name=node.name))
    return shapes


def _collisions() -> dict[frozenset[str], list[_Declaration]]:
    """Return the shapes declared at two or more distinct sites."""
    return {
        fields: sorted(declarations, key=lambda d: (d.module, d.lineno))
        for fields, declarations in _shapes_by_fields().items()
        if len({(d.module, d.name) for d in declarations}) > 1
    }


def _render(fields: frozenset[str], declarations: list[_Declaration]) -> str:
    """Return a copy-pasteable description of one collision."""
    joined = ", ".join(sorted(fields))
    sites = "\n      ".join(str(d) for d in declarations)
    return f"  ({joined})\n      {sites}"


def test_no_new_unallowlisted_field_shape_collisions() -> None:
    """A recurring field shape must be one type, not one type per module.

    Load-bearing. Three ways to fail, and each one is a real regression:
    a shape that recurs without an allowlist entry, a class that joins an
    already-allowlisted shape, and an entry that outlived its collision.
    """
    allowed = {entry.fields: entry for entry in _ALLOWED_SHAPES}
    collisions = _collisions()
    problems: list[str] = []

    for fields, declarations in sorted(collisions.items(), key=lambda kv: sorted(kv[0])):
        entry = allowed.get(fields)
        if entry is None:
            problems.append("not allowlisted:\n" + _render(fields, declarations))
            continue
        declared = {declaration.name for declaration in declarations}
        if declared != entry.classes:
            joined = ", ".join(sorted(declared - entry.classes)) or "(none)"
            problems.append(f"allowlisted shape gained a class ({joined}):\n" + _render(fields, declarations))

    for entry in _ALLOWED_SHAPES:
        if entry.fields not in collisions:
            problems.append(
                "stale allowlist entry - this shape no longer collides, delete it:\n"
                f"  ({', '.join(sorted(entry.fields))})"
            )

    assert not problems, (
        "result-class field shapes are out of step with the allowlist:\n\n"
        + "\n\n".join(problems)
        + "\n\nDeclare the shape once and specialise it, or - if the split is "
        "deliberate, as a wire DTO next to its internal type is - add an entry "
        "to _DELIBERATE_SHAPES with the reason it must stay two types."
    )


def test_every_allowlist_entry_states_a_reason() -> None:
    """An entry without a reason is a silenced failure, not a decision."""
    unexplained = [", ".join(sorted(entry.fields)) for entry in _ALLOWED_SHAPES if len(entry.reason.strip()) < 40]
    assert not unexplained, (
        "allowlist entries with no stated reason:\n  "
        + "\n  ".join(unexplained)
        + "\n\nThe reason is the whole value of the entry: it says whether the "
        "split is deliberate or is debt someone still has to pay."
    )


def test_sbom_finding_and_response_dto_stay_deliberately_split() -> None:
    """The one deliberate split keeps its reason and does not quietly vanish.

    ``SBOMVulnFinding`` is what the scanner produces; ``SBOMVulnFindingResponse``
    is what the HTTP route returns. They share a field set today and that is
    correct - the wire shape is a contract with its own compatibility rules.
    If someone collapses them to remove a duplicate, this test says why not.
    """
    entries = [
        entry
        for entry in _DELIBERATE_SHAPES
        if entry.classes == frozenset({"SBOMVulnFinding", "SBOMVulnFindingResponse"})
    ]
    assert len(entries) == 1, "the SBOM finding/DTO split must be recorded exactly once in _DELIBERATE_SHAPES"
    assert "wire contract" in entries[0].reason, "the entry must say why the split is deliberate, not merely that it is"

    declared = {
        declaration.name: declaration.module
        for declarations in _collisions().values()
        for declaration in declarations
        if declaration.name in entries[0].classes
    }
    assert declared == {
        "SBOMVulnFinding": "core/security/sbom.py",
        "SBOMVulnFindingResponse": "core/routes/sbom.py",
    }, f"the deliberately split pair moved or was collapsed: {declared}"


def test_verify_result_subclasses_expose_ok_reason_receipt() -> None:
    """Every migrated receipt verifier answers "did it pass, and why" the same way.

    The eight names survive as specialisations of the one base type: a
    specialisation of a generic dataclass is that base class with its receipt
    slot pinned, so each name still says which receipt it carries. The property
    under test is that a caller holding any of them can read ``ok``, ``reason``
    and ``receipt`` without knowing which verifier produced it.
    """
    from bernstein.core.cost.scheduling.receipt import DispatchVerifyResult
    from bernstein.core.orchestration.escalation import EscalationVerifyResult
    from bernstein.core.protocols.payments.x402 import SpendVerifyResult
    from bernstein.core.review.receipt import AutofixVerifyResult
    from bernstein.core.sandbox.pool_placement import PlacementVerifyResult
    from bernstein.core.skills.provenance import InstallVerifyResult
    from bernstein.core.verify_result import VerifyResult
    from bernstein.eval.gate_receipt import VerdictVerifyResult
    from bernstein.eval.promotion import RevocationVerifyResult

    specialisations = {
        "DispatchVerifyResult": DispatchVerifyResult,
        "EscalationVerifyResult": EscalationVerifyResult,
        "SpendVerifyResult": SpendVerifyResult,
        "AutofixVerifyResult": AutofixVerifyResult,
        "PlacementVerifyResult": PlacementVerifyResult,
        "InstallVerifyResult": InstallVerifyResult,
        "VerdictVerifyResult": VerdictVerifyResult,
        "RevocationVerifyResult": RevocationVerifyResult,
    }

    wrong_base = {
        name: get_origin(alias) for name, alias in specialisations.items() if get_origin(alias) is not VerifyResult
    }
    assert not wrong_base, f"names that no longer specialise VerifyResult: {wrong_base}"

    for name, alias in specialisations.items():
        outcome = alias(ok=False, reason="tampered", receipt=None)
        assert isinstance(outcome, VerifyResult), f"{name} constructed something other than a VerifyResult"
        assert (outcome.ok, outcome.reason, outcome.receipt) == (
            False,
            "tampered",
            None,
        ), name


def test_verify_result_is_frozen_so_a_failed_verification_cannot_be_flipped() -> None:
    """A verdict a caller can rewrite in place is not a verdict."""
    from bernstein.core.verify_result import VerifyResult

    outcome: VerifyResult[str] = VerifyResult(ok=False, reason="signature does not verify")
    with pytest.raises(FrozenInstanceError):
        outcome.ok = True  # type: ignore[misc]
