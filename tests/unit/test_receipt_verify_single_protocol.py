"""One receipt protocol: one sign/verify pair, one canonicalisation (#5096).

Eight modules each grew their own ``verify_receipt``, four their own
``sign_receipt`` and four their own ``canonical_receipt_bytes``. A holder of a
receipt had to know which module produced it before it could be checked, and
"receipt verified" meant eight different things.

The guards below pin the direction of travel: every one of those three names is
defined once in :mod:`bernstein.core.receipts.protocol`, plus a shrinking
allowlist of modules whose migration has not landed yet. Adding a ninth
verifier fails immediately; migrating one requires deleting its allowlist entry
(``test_pending_*_allowlist_has_no_stale_entries`` fails otherwise). When the
allowlists are empty they - and these two stale-entry guards - go away.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.receipts.protocol import (
    CANONICALIZATION_V1,
    DuplicateReceiptKindError,
    ReceiptEnvelope,
    UnknownReceiptKindError,
    canonical_receipt_bytes,
    register_receipt_kind,
    registered_kinds,
    sign_receipt,
    verify_receipt,
)
from bernstein.core.skills.catalog.signature import generate_signer_keypair

_SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"

#: The one module allowed to define the protocol's three names.
PROTOCOL_MODULE = "core/receipts/protocol.py"

#: Modules whose verifier has not been migrated onto the protocol yet (#5096
#: slice 3). Delete an entry when its module stops defining the name; never
#: add one.
PENDING_VERIFY_RECEIPT = frozenset(
    {
        "core/admission/receipts.py",
        "core/orchestration/sla_receipt.py",
        "core/orchestration/supervisor_receipt.py",
        "core/payments/receipt.py",
        "core/persistence/journal_export.py",
        "core/sandbox/selection_receipt.py",
    },
)

PENDING_SIGN_RECEIPT = frozenset(
    {
        "core/admission/receipts.py",
        "core/orchestration/sla_receipt.py",
        "core/orchestration/supervisor_receipt.py",
        "core/sandbox/selection_receipt.py",
    },
)

PENDING_CANONICAL_RECEIPT_BYTES = frozenset(
    {
        "core/admission/receipts.py",
        "core/orchestration/sla_receipt.py",
        "core/orchestration/supervisor_receipt.py",
        "core/sandbox/selection_receipt.py",
    },
)


#: HTTP verbs a router or app decorator registers a request handler under.
_ROUTE_DECORATOR_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})


def _is_route_handler(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when the function is registered as an HTTP endpoint.

    A route handler is named after the URL it serves, so ``POST
    /governance/verify-receipt`` is a function called ``verify_receipt`` that
    verifies nothing itself: it reads the request body and delegates. Counting
    it would fail the guard over a module that has no verifier to migrate.

    The exclusion cannot hide a real verifier. A verifier is not registered
    under an HTTP verb, and the stale-entry guards below fail the moment this
    stops seeing a verifier that is still on an allowlist.
    """
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATOR_METHODS:
            return True
    return False


def _definition_sites(name: str) -> set[str]:
    """Return package-relative paths of every ``def <name>`` under the package.

    HTTP route handlers are skipped: they expose a verifier over a URL rather
    than being one.
    """
    sites: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == name
                and not _is_route_handler(node)
            ):
                sites.add(path.relative_to(_SRC).as_posix())
    return sites


# ---------------------------------------------------------------------------
# Guards: one definition each, outside the shrinking migration allowlist
# ---------------------------------------------------------------------------


def test_exactly_one_verify_receipt_definition() -> None:
    """Only the protocol module defines ``verify_receipt`` (bar the allowlist)."""
    sites = _definition_sites("verify_receipt")
    assert sites - PENDING_VERIFY_RECEIPT == {PROTOCOL_MODULE}, (
        f"verify_receipt must be defined once, in {PROTOCOL_MODULE}; found {sorted(sites)}"
    )


def test_exactly_one_sign_receipt_definition() -> None:
    """Only the protocol module defines ``sign_receipt`` (bar the allowlist)."""
    sites = _definition_sites("sign_receipt")
    assert sites - PENDING_SIGN_RECEIPT == {PROTOCOL_MODULE}, (
        f"sign_receipt must be defined once, in {PROTOCOL_MODULE}; found {sorted(sites)}"
    )


def test_exactly_one_canonical_receipt_bytes_definition() -> None:
    """Only the protocol module defines ``canonical_receipt_bytes``."""
    sites = _definition_sites("canonical_receipt_bytes")
    assert sites - PENDING_CANONICAL_RECEIPT_BYTES == {PROTOCOL_MODULE}, (
        f"canonical_receipt_bytes must be defined once, in {PROTOCOL_MODULE}; found {sorted(sites)}"
    )


def test_pending_verify_receipt_allowlist_has_no_stale_entries() -> None:
    """A migrated module must be struck from the allowlist, not left in it."""
    stale = PENDING_VERIFY_RECEIPT - _definition_sites("verify_receipt")
    assert stale == set(), (
        f"these modules no longer define verify_receipt; drop them from the allowlist: {sorted(stale)}"
    )


def test_pending_sign_and_canonical_allowlists_have_no_stale_entries() -> None:
    """The sign/canonicalise allowlists shrink with their migrations too."""
    stale_sign = PENDING_SIGN_RECEIPT - _definition_sites("sign_receipt")
    stale_canonical = PENDING_CANONICAL_RECEIPT_BYTES - _definition_sites("canonical_receipt_bytes")
    assert stale_sign == set(), f"drop from the sign_receipt allowlist: {sorted(stale_sign)}"
    assert stale_canonical == set(), f"drop from the canonical_receipt_bytes allowlist: {sorted(stale_canonical)}"


def test_route_handlers_are_not_counted_as_verifier_definitions() -> None:
    """The endpoint exposing a verifier is not itself a second verifier.

    Narrow by construction: only a function registered under an HTTP verb is
    skipped. A plain function of the same name still counts, so a real ninth
    verifier cannot slip past the guards above by sharing a route's name.
    """
    module = ast.parse(
        "import bernstein\n"
        "@router.post('/governance/verify-receipt')\n"
        "async def verify_receipt(request): ...\n"
        "@staticmethod\n"
        "def sign_receipt(payload): ...\n"
        "def canonical_receipt_bytes(payload): ...\n",
    )
    handlers = {
        node.name: _is_route_handler(node)
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert handlers == {
        "verify_receipt": True,
        "sign_receipt": False,
        "canonical_receipt_bytes": False,
    }


# ---------------------------------------------------------------------------
# The one protocol: registered kinds round-trip, tampering is caught
# ---------------------------------------------------------------------------


def _change_payload() -> dict[str, Any]:
    from bernstein.core.security.change_receipt import ChangeAttempt, ChangeReceipt

    return ChangeReceipt(
        plan_id="plan-abc123",
        plan_digest="a" * 64,
        playbook_digest="b" * 64,
        environment_digest="c" * 64,
        approver_identity="alice@example.com",
        changes=(
            ChangeAttempt(
                change_id="change-001",
                change_type="create",
                target="iam.User:alice",
                attempted_at="2025-01-15T10:30:00Z",
                outcome="success",
            ),
        ),
        final_status="complete",
        timestamp="2025-01-15T10:31:00Z",
    ).to_dict()


def _recovery_payload() -> dict[str, Any]:
    from bernstein.core.planning.recovery_receipt import RecoveryReceipt

    return RecoveryReceipt(
        failing_node_id="run-tests",
        recovery_node_id="fix-bugs",
        source_status="failed",
        condition_context={"status": "failed", "result": "2 tests failed"},
        gate_report=({"gate": "tests", "passed": False, "blocked": True, "detail": "2 failed"},),
        journal_tail=({"event": "task_failed"},),
    ).canonical_payload()


#: One payload fixture per registered kind. A kind registered without a fixture
#: fails ``test_every_registered_kind_round_trips``.
KIND_PAYLOADS: dict[str, Any] = {
    "security.change": _change_payload,
    "planning.recovery": _recovery_payload,
}


def _keys() -> tuple[str, str]:
    private_pem, public_pem = generate_signer_keypair()
    return private_pem, public_pem


def test_every_registered_kind_round_trips() -> None:
    """Sign then verify succeeds for every registered kind, one function each."""
    private_pem, public_pem = _keys()
    assert set(registered_kinds()) == set(KIND_PAYLOADS), (
        "every registered receipt kind needs a payload fixture here: "
        f"registered={sorted(registered_kinds())} fixtures={sorted(KIND_PAYLOADS)}"
    )

    for kind, build in KIND_PAYLOADS.items():
        envelope = sign_receipt(kind, build(), private_key_pem=private_pem, public_key_pem=public_pem)
        result = verify_receipt(envelope)
        assert result.ok, f"{kind} did not verify: {result.errors}"
        assert result.kind == kind


def test_tampered_payload_fails_for_every_kind() -> None:
    """One mutated payload field is rejected for every kind, not just some."""
    private_pem, public_pem = _keys()
    for kind, build in KIND_PAYLOADS.items():
        envelope = sign_receipt(kind, build(), private_key_pem=private_pem, public_key_pem=public_pem)
        tampered_payload = copy.deepcopy(envelope.payload)
        first_key = sorted(tampered_payload)[0]
        tampered_payload[first_key] = "tampered"
        tampered = ReceiptEnvelope(
            kind=envelope.kind,
            payload=tampered_payload,
            payload_digest=envelope.payload_digest,
            canonicalization=envelope.canonicalization,
            signature=envelope.signature,
            public_key_pem=envelope.public_key_pem,
        )
        result = verify_receipt(tampered)
        assert not result.ok, f"{kind} accepted a tampered payload"
        assert any("payload_digest" in e for e in result.errors), result.errors


def test_receipt_of_unknown_kind_is_reported_not_raised() -> None:
    """An unrecognised kind reports a verification failure, never a KeyError."""
    private_pem, public_pem = _keys()
    envelope = sign_receipt(
        "security.change",
        _change_payload(),
        private_key_pem=private_pem,
        public_key_pem=public_pem,
    )
    document = envelope.to_dict()
    document["kind"] = "not.a.registered.kind"

    result = verify_receipt(document)
    assert not result.ok
    assert any("unrecognised receipt kind" in e for e in result.errors), result.errors


def test_signature_binds_the_kind_to_the_payload() -> None:
    """A payload signed under one kind does not verify under another."""
    private_pem, public_pem = _keys()
    envelope = sign_receipt(
        "security.change",
        _change_payload(),
        private_key_pem=private_pem,
        public_key_pem=public_pem,
    )
    relabelled = ReceiptEnvelope(
        kind="planning.recovery",
        payload=envelope.payload,
        payload_digest=envelope.payload_digest,
        canonicalization=envelope.canonicalization,
        signature=envelope.signature,
        public_key_pem=envelope.public_key_pem,
    )

    result = verify_receipt(relabelled)
    assert not result.ok
    assert any("signature" in e for e in result.errors), result.errors


def test_duplicate_kind_registration_raises() -> None:
    """Registering a kind twice fails where it happens: at import time."""
    with pytest.raises(DuplicateReceiptKindError):
        register_receipt_kind("security.change", payload_check=lambda _payload: ())


def test_signing_an_unregistered_kind_raises() -> None:
    """Signing refuses a kind the registry does not know."""
    private_pem, public_pem = _keys()
    with pytest.raises(UnknownReceiptKindError):
        sign_receipt("no.such.kind", {}, private_key_pem=private_pem, public_key_pem=public_pem)


def test_unknown_canonicalization_is_rejected() -> None:
    """A receipt canonicalised by an unknown rule cannot be declared verified."""
    private_pem, public_pem = _keys()
    envelope = sign_receipt(
        "security.change",
        _change_payload(),
        private_key_pem=private_pem,
        public_key_pem=public_pem,
    )
    assert envelope.canonicalization == CANONICALIZATION_V1
    document = envelope.to_dict()
    document["canonicalization"] = "receipt-canonical-json/v99"

    result = verify_receipt(document)
    assert not result.ok
    assert any("canonicalization" in e for e in result.errors), result.errors


def test_canonical_receipt_bytes_is_key_order_stable() -> None:
    """The one canonicaliser ignores key order, so two writers byte-agree."""
    payload = _change_payload()
    reordered = dict(reversed(list(payload.items())))
    assert canonical_receipt_bytes(payload) == canonical_receipt_bytes(reordered)
