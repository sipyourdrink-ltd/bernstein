"""Executable conformance corpus for the audit receipt formats (issue #4987).

The standalone verifier (``tools/verify_audit_receipt.py``, issue #4976) makes
a receipt checkable by a reader who does not run bernstein. That is necessary
but not sufficient for the format to be useful beyond this project: nothing
states what an implementation must do to produce a receipt a conforming
verifier accepts, or what a conforming verifier must reject. Without a
corpus, "produces a valid receipt" is defined by whatever this project's own
writer happens to emit -- a format defined by one implementation is an
implementation detail with a schema attached.

This module is the executable half of that: a small, numbered set of
:data:`REQUIREMENTS` (each traceable to a specific check the standalone
verifier performs) and, for every requirement, at least one positive corpus
case (a receipt that satisfies it) and at least one negative case (a receipt
that deliberately violates only that requirement, everything else left
intact). :func:`assert_corpus_completeness` enforces that pairing so adding a
requirement without a matching case is a build-time failure, not a silent gap.

Two independent uses of the corpus:

* :func:`run_corpus` -- verifier conformance. Feeds every corpus case to a
  given verifier implementation (subprocess, ``--receipt``/``--format``) and
  reports, per requirement, whether the verifier's accept/reject verdict
  matched what the case expects. Pointed at this project's own standalone
  verifier, our writer gets no privileged path: the same records a foreign
  verifier would be checked against are what ours is checked against too.
* :func:`evaluate_receipt` -- producer conformance. Feeds one receipt of
  unknown provenance (a "synthetic producer" in the test suite, or any
  operator-supplied file at the CLI) through every requirement's check and
  reports acceptance per requirement, so a non-conforming receipt names the
  specific requirement it violates instead of a single pass/fail bit.

Positive cases and the base valid receipt are minted with a corpus-local,
throwaway Ed25519 key over two hand-built synthetic events -- no wall clock,
no dependency on any fixture under ``tests/``. Negative cases are built two
ways: corrupting a signature byte (breaks that format's own signature check),
or, for the requirements distinct from plain signature validity, re-signing a
deliberately wrong payload/subject/root with the same corpus key so the
signature verifies while the bound content does not match -- the failure mode
a verifier that only checks "is this signed" rather than "is this signed AND
bound to the right thing" would miss.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cbor2

from bernstein.core.security.audit_receipt import (
    ALL_FORMATS,
    _build_cose_sign1,
    _build_intoto_envelope,
    _canonical_json_bytes,
    materialize_receipt,
    rebuild_receipt_range,
)
from bernstein.core.security.key_custody import FileBasedKMSAdapter
from bernstein.core.verifier.audit_receipt_verifier import _VERIFIER_SCRIPT

if TYPE_CHECKING:
    from bernstein.core.security.key_custody import KMSAdapter

__all__ = [
    "REQUIREMENTS",
    "AuditReceiptConformanceError",
    "CaseResult",
    "ConformanceRun",
    "CorpusCase",
    "Requirement",
    "RequirementVerdict",
    "assert_corpus_completeness",
    "build_corpus",
    "default_verifier_path",
    "evaluate_receipt",
    "run_corpus",
]


class AuditReceiptConformanceError(RuntimeError):
    """Raised when the corpus does not cover every numbered requirement."""


def default_verifier_path() -> Path:
    """Return the repo's own standalone verifier -- the default implementation."""
    return _VERIFIER_SCRIPT


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """One numbered, stable conformance requirement.

    Attributes:
        id: Stable identifier (e.g. ``"AR-COSE-002"``). Never renumbered --
            a retired requirement is marked so in ``statement``, not deleted,
            so a case naming it in old evidence still resolves.
        applies_to: Which ``--format`` argument exercises this requirement
            against ``tools/verify_audit_receipt.py`` (``"all"`` for a
            requirement that binds across every format).
        statement: The conformance rule in prose.
    """

    id: str
    applies_to: str
    statement: str


#: Format argument to pass to a ``--receipt``/``--format`` verifier for each
#: requirement family.
_FORMAT_ARG: dict[str, str] = {
    "shared": "all",
    "cose": "cose",
    "intoto": "intoto",
    "transparency": "transparency",
}

#: The standalone verifier's own named check (``tools/verify_audit_receipt.py``
#: ``CheckResult.name``) that each requirement family's property is checked
#: by. Used only by :func:`evaluate_receipt` to attribute a rejection to a
#: requirement by parsing the reference verifier's per-check output lines --
#: see that function's docstring for why the overall exit code alone cannot
#: do this (the subject-binding check runs unconditionally and folds into
#: every ``--format`` invocation's exit code, so exit-code-only attribution
#: would blame every requirement whenever the subject digest is wrong).
_CHECK_NAME: dict[str, str] = {
    "shared": "subject_binding",
    "cose": "cose",
    "intoto": "intoto",
    "transparency": "transparency",
}

REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "AR-SUBJECT-001",
        "shared",
        "receipt.subject.digest.sha256 MUST equal SHA-256 of the canonical "
        "JSONL of receipt.events (the chain-range head_sha256).",
    ),
    Requirement(
        "AR-TAMPER-001",
        "shared",
        "A receipt whose embedded events no longer hash to "
        "receipt.subject.digest.sha256 MUST be rejected by every format "
        "present in the receipt, not only by the subject-binding check.",
    ),
    Requirement(
        "AR-COSE-001",
        "cose",
        "formats.cose.cose_sign1_b64 MUST decode to a COSE_Sign1 (RFC 9052) "
        "whose EdDSA signature verifies under the embedded public key.",
    ),
    Requirement(
        "AR-COSE-002",
        "cose",
        "The COSE_Sign1 payload MUST equal the raw 32 bytes of the receipt "
        "subject digest -- a validly signed COSE object over any other "
        "payload MUST be rejected.",
    ),
    Requirement(
        "AR-INTOTO-001",
        "intoto",
        "formats.intoto MUST carry a DSSE (in-toto v1) signature that "
        "verifies under the embedded public key over the DSSE PAE.",
    ),
    Requirement(
        "AR-INTOTO-002",
        "intoto",
        "The in-toto Statement's subject digest.sha256 MUST equal the "
        "receipt subject digest -- a validly signed statement over any "
        "other subject MUST be rejected.",
    ),
    Requirement(
        "AR-TRANSPARENCY-001",
        "transparency",
        "formats.transparency.signed_tree_head MUST carry an EdDSA "
        "signature that verifies over its own canonical JSON bytes.",
    ),
    Requirement(
        "AR-TRANSPARENCY-002",
        "transparency",
        "The Merkle root recomputed from the embedded events MUST equal "
        "signed_tree_head.root_hash, and the inclusion proof MUST fold the "
        "chain-head leaf up to that same root.",
    ),
)


def assert_corpus_completeness(
    requirements: tuple[Requirement, ...],
    corpus: tuple[CorpusCase, ...],
) -> None:
    """Raise unless every requirement has >=1 positive and >=1 negative case.

    This is what makes "add a requirement without a corpus case" a build-time
    failure: :func:`build_corpus` calls this on its own output before
    returning it, so an incomplete corpus never reaches a caller.
    """
    have_positive: set[str] = set()
    have_negative: set[str] = set()
    for case in corpus:
        (have_positive if case.expect_valid else have_negative).add(case.requirement_id)
    missing = [
        f"{req.id} (needs {'positive' if req.id not in have_positive else 'negative'} case)"
        for req in requirements
        if req.id not in have_positive or req.id not in have_negative
    ]
    if missing:
        raise AuditReceiptConformanceError(
            "requirement(s) missing a positive and/or negative corpus case: " + "; ".join(missing),
        )


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------

#: Two hand-built synthetic events -- fixed timestamps, no wall clock, no
#: dependency on any fixture under tests/. Shape matches what
#: ``_rebuild_slice_chain`` reads (timestamp/event_type/actor/resource_type/
#: resource_id/details); the source-chain ``hmac`` witness field is omitted,
#: which ``_rebuild_slice_chain`` treats as an empty witness.
_EVENTS: tuple[dict[str, Any], ...] = (
    {
        "timestamp": "2020-01-01T00:00:00.000000Z",
        "event_type": "task.created",
        "actor": "alice",
        "resource_type": "task",
        "resource_id": "T-1",
        "details": {"role": "backend"},
    },
    {
        "timestamp": "2020-01-01T00:00:01.000000Z",
        "event_type": "task.completed",
        "actor": "alice",
        "resource_type": "task",
        "resource_id": "T-1",
        "details": {"status": "ok"},
    },
)
_HMAC_KEY: bytes = b"c" * 32
_SIGN_SEED: bytes = b"c" * 32
_SINCE: str = "2020-01-01T00:00:00.000000Z"
_UNTIL: str = "2020-01-01T00:00:02.000000Z"
#: Deliberately wrong 32-byte digests / 32-byte hex strings used by the
#: "validly signed, wrongly bound" negative cases below. Neither equals the
#: real head_sha256 the corpus-local events produce.
_WRONG_HEX_A: str = "1" * 64
_WRONG_HEX_B: str = "2" * 64


def _kms_adapter(tmp_dir: Path) -> KMSAdapter:
    """A throwaway, corpus-local Ed25519 signer (key never leaves this call).

    The seed is handed to the custody boundary as a raw 32-byte key file --
    one of the two shapes :class:`FileBasedKMSAdapter` already accepts -- so
    the corpus obtains its signer the same way every other signing surface
    does and holds no private key material of its own. An operator who moves
    the signing key to another backend moves one adapter, not each caller.
    """
    key_path = tmp_dir / "conformance-sign.key"
    key_path.write_bytes(_SIGN_SEED)
    # FileBasedKMSAdapter reads the key eagerly at construction, so the key
    # material is safe to use after tmp_dir is cleaned up.
    return FileBasedKMSAdapter(key_path, kid="audit-receipt-conformance-key")


def _base_receipt(kms: KMSAdapter) -> dict[str, Any]:
    """The one genuinely valid receipt every positive case reuses."""
    rebuilt, head_hmac, head_sha256 = rebuild_receipt_range(list(_EVENTS), _HMAC_KEY)
    result = materialize_receipt(
        Path("."),  # unused: write=False never touches this
        since=_SINCE,
        until=_UNTIL,
        rebuilt=rebuilt,
        head_hmac=head_hmac,
        head_sha256=head_sha256,
        kms_adapter=kms,
        requested=ALL_FORMATS,
        subject_name="audit-receipt-conformance-corpus",
        online_rekor=False,
        output_dir=None,
        write=False,
    )
    return result.receipt


def _corrupt_signature_b64(base64_sig: str) -> str:
    """Flip the last byte of a base64-encoded signature."""
    raw = bytearray(base64.b64decode(base64_sig))
    raw[-1] ^= 0xFF
    return base64.b64encode(bytes(raw)).decode("ascii")


def _corrupt_cose_signature(base: dict[str, Any]) -> dict[str, Any]:
    """AR-COSE-001 negative: a COSE_Sign1 whose signature does not verify."""
    r = copy.deepcopy(base)
    block = r["formats"]["cose"]
    obj = cbor2.loads(base64.b64decode(block["cose_sign1_b64"]))
    protected_bstr, unprotected, payload, signature = obj.value
    corrupted = bytearray(signature)
    corrupted[-1] ^= 0xFF
    new_obj = cbor2.CBORTag(obj.tag, [protected_bstr, unprotected, payload, bytes(corrupted)])
    block["cose_sign1_b64"] = base64.b64encode(cbor2.dumps(new_obj, canonical=True)).decode("ascii")
    return r


def _cose_payload_mismatch(base: dict[str, Any], kms: KMSAdapter, key_id: str) -> dict[str, Any]:
    """AR-COSE-002 negative: validly signed COSE_Sign1 over the wrong payload."""
    r = copy.deepcopy(base)
    r["formats"]["cose"] = _build_cose_sign1(
        subject_digest_bytes=bytes.fromhex(_WRONG_HEX_A),
        key_id=key_id,
        kms_adapter=kms,
    )
    return r


def _corrupt_intoto_signature(base: dict[str, Any]) -> dict[str, Any]:
    """AR-INTOTO-001 negative: a DSSE envelope whose signature does not verify."""
    r = copy.deepcopy(base)
    sig = r["formats"]["intoto"]["signatures"][0]
    sig["sig"] = _corrupt_signature_b64(sig["sig"])
    return r


def _intoto_subject_mismatch(base: dict[str, Any], kms: KMSAdapter, key_id: str) -> dict[str, Any]:
    """AR-INTOTO-002 negative: validly signed statement over the wrong subject."""
    r = copy.deepcopy(base)
    r["formats"]["intoto"] = _build_intoto_envelope(
        subject_name=base["subject"]["name"],
        head_sha256=_WRONG_HEX_A,
        range_block=base["range"],
        key_id=key_id,
        kms_adapter=kms,
    )
    return r


def _corrupt_transparency_signature(base: dict[str, Any]) -> dict[str, Any]:
    """AR-TRANSPARENCY-001 negative: a signed tree head whose signature does not verify."""
    r = copy.deepcopy(base)
    sth = r["formats"]["transparency"]["signed_tree_head"]
    sth["signature_b64"] = _corrupt_signature_b64(sth["signature_b64"])
    return r


def _transparency_root_mismatch(base: dict[str, Any], kms: KMSAdapter) -> dict[str, Any]:
    """AR-TRANSPARENCY-002 negative: validly signed STH naming the wrong root.

    The inclusion proof is left untouched, so it still folds to the *old*
    (correct) root -- which now disagrees with the new, validly signed
    ``root_hash``. Either comparison alone is enough to reject this receipt.
    """
    r = copy.deepcopy(base)
    sth = r["formats"]["transparency"]["signed_tree_head"]
    tampered = {"tree_size": sth["tree_size"], "root_hash": _WRONG_HEX_B, "subject_sha256": sth["subject_sha256"]}
    signature = kms.sign(_canonical_json_bytes(tampered))
    sth["root_hash"] = _WRONG_HEX_B
    sth["signature_b64"] = base64.b64encode(signature).decode("ascii")
    return r


def _tamper_subject_digest(base: dict[str, Any]) -> dict[str, Any]:
    """AR-SUBJECT-001 negative: subject digest no longer matches the events."""
    r = copy.deepcopy(base)
    r["subject"]["digest"]["sha256"] = _WRONG_HEX_A
    return r


def _tamper_embedded_event(base: dict[str, Any]) -> dict[str, Any]:
    """AR-TAMPER-001 negative: one embedded event mutated after signing."""
    r = copy.deepcopy(base)
    r["events"][0]["details"] = {**r["events"][0]["details"], "role": "TAMPERED"}
    return r


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One corpus record: a receipt, the requirement it targets, and the
    accept/reject verdict a conforming verifier must reach for it."""

    case_id: str
    requirement_id: str
    expect_valid: bool
    verify_format: str
    receipt: dict[str, Any]


def build_corpus() -> tuple[CorpusCase, ...]:
    """Build the full corpus: one positive case per requirement, plus the
    per-requirement negative cases above. Raises
    :class:`AuditReceiptConformanceError` if the result would not actually
    cover every entry in :data:`REQUIREMENTS`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        kms = _kms_adapter(Path(tmp))
        base = _base_receipt(kms)
        key_id = str(kms.public_key_jwk().get("kid") or "audit-receipt-conformance-key")

        cases: list[CorpusCase] = [
            CorpusCase(f"{req.id}-positive", req.id, True, _FORMAT_ARG[req.applies_to], base) for req in REQUIREMENTS
        ]
        cases += [
            CorpusCase("AR-SUBJECT-001-negative", "AR-SUBJECT-001", False, "all", _tamper_subject_digest(base)),
            CorpusCase("AR-TAMPER-001-negative", "AR-TAMPER-001", False, "all", _tamper_embedded_event(base)),
            CorpusCase("AR-COSE-001-negative", "AR-COSE-001", False, "cose", _corrupt_cose_signature(base)),
            CorpusCase("AR-COSE-002-negative", "AR-COSE-002", False, "cose", _cose_payload_mismatch(base, kms, key_id)),
            CorpusCase("AR-INTOTO-001-negative", "AR-INTOTO-001", False, "intoto", _corrupt_intoto_signature(base)),
            CorpusCase(
                "AR-INTOTO-002-negative",
                "AR-INTOTO-002",
                False,
                "intoto",
                _intoto_subject_mismatch(base, kms, key_id),
            ),
            CorpusCase(
                "AR-TRANSPARENCY-001-negative",
                "AR-TRANSPARENCY-001",
                False,
                "transparency",
                _corrupt_transparency_signature(base),
            ),
            CorpusCase(
                "AR-TRANSPARENCY-002-negative",
                "AR-TRANSPARENCY-002",
                False,
                "transparency",
                _transparency_root_mismatch(base, kms),
            ),
        ]

    corpus = tuple(cases)
    assert_corpus_completeness(REQUIREMENTS, corpus)
    return corpus


# ---------------------------------------------------------------------------
# Running the corpus against an implementation
# ---------------------------------------------------------------------------


def _invoke_verifier(
    verifier_path: Path,
    receipt_path: Path,
    verify_format: str,
    *,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Run ``verifier_path`` as a subprocess against one receipt file.

    Every implementation is invoked the same way this project's own
    standalone verifier is: ``<verifier> --receipt <path> --format <fmt>``,
    exit 0 = accept, nonzero = reject. A ``.py`` verifier runs under this
    interpreter; anything else runs directly, so a non-Python implementation
    that speaks the same two flags can be pointed at the corpus too.
    """
    argv = [sys.executable, str(verifier_path)] if verifier_path.suffix == ".py" else [str(verifier_path)]
    argv += ["--receipt", str(receipt_path), "--format", verify_format]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"verifier timed out after {timeout}s"
    return proc.returncode == 0, f"exit={proc.returncode}"


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's outcome against one implementation."""

    case_id: str
    requirement_id: str
    expect_valid: bool
    actual_valid: bool
    detail: str

    @property
    def conformant(self) -> bool:
        """Whether the implementation reached the verdict the case expects."""
        return self.actual_valid == self.expect_valid


@dataclass(frozen=True, slots=True)
class RequirementVerdict:
    """Aggregate verdict for one requirement across all its cases."""

    requirement_id: str
    case_results: tuple[CaseResult, ...]

    @property
    def conformant(self) -> bool:
        return all(c.conformant for c in self.case_results)


@dataclass(frozen=True, slots=True)
class ConformanceRun:
    """Full per-requirement report for one implementation run."""

    verifier: str
    verdicts: tuple[RequirementVerdict, ...]

    @property
    def ok(self) -> bool:
        return all(v.conformant for v in self.verdicts)


def run_corpus(
    corpus: tuple[CorpusCase, ...],
    *,
    verifier_path: Path,
    tmp_dir: Path,
) -> ConformanceRun:
    """Verifier conformance: run every corpus case against ``verifier_path``.

    A requirement is conformant only if the verifier reaches the expected
    verdict on *every* one of its cases -- accepting even one negative case,
    or rejecting a positive one, marks that requirement (and the run) as
    non-conformant.
    """
    by_requirement: dict[str, list[CaseResult]] = {}
    for case in corpus:
        receipt_path = tmp_dir / f"{case.case_id}.json"
        receipt_path.write_text(json.dumps(case.receipt), encoding="utf-8")
        actual_valid, detail = _invoke_verifier(verifier_path, receipt_path, case.verify_format)
        by_requirement.setdefault(case.requirement_id, []).append(
            CaseResult(case.case_id, case.requirement_id, case.expect_valid, actual_valid, detail),
        )
    verdicts = tuple(RequirementVerdict(rid, tuple(results)) for rid, results in sorted(by_requirement.items()))
    return ConformanceRun(str(verifier_path), verdicts)


#: Matches one of the reference verifier's ``_print_check`` lines, e.g.
#: ``"[FAIL] cose - COSE_Sign1 signature does not verify"``.
_CHECK_LINE_RE = re.compile(r"^\[(PASS|FAIL)\]\s+(\S+)(?:\s+-\s+(.*))?$")


def _run_verifier_verbose(
    verifier_path: Path, receipt_path: Path, *, timeout: float = 30.0
) -> dict[str, tuple[bool, str]]:
    """Run the verifier once with ``--format all --verbose`` and parse its
    per-check ``[PASS|FAIL] <name>`` lines into ``{name: (ok, detail)}``.

    Relies on the reference verifier's documented, stable line format
    (:func:`tools.verify_audit_receipt._print_check`); an implementation that
    does not print this convention simply yields no parsed checks, and every
    requirement is reported unattributed (see :func:`evaluate_receipt`).
    """
    argv = [sys.executable, str(verifier_path)] if verifier_path.suffix == ".py" else [str(verifier_path)]
    argv += ["--receipt", str(receipt_path), "--format", "all", "--verbose"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {}
    checks: dict[str, tuple[bool, str]] = {}
    for line in proc.stdout.splitlines():
        m = _CHECK_LINE_RE.match(line.strip())
        if m:
            checks[m.group(2)] = (m.group(1) == "PASS", m.group(3) or "")
    return checks


def evaluate_receipt(
    receipt: dict[str, Any],
    *,
    verifier_path: Path,
    tmp_dir: Path,
) -> tuple[CaseResult, ...]:
    """Producer conformance: check one receipt of unknown provenance.

    Unlike :func:`run_corpus`, this receipt carries no corpus label -- it is
    a candidate output from some producer (a synthetic non-conforming one in
    tests, an operator-supplied file at the CLI). A rejection is attributed
    to a requirement by parsing the reference verifier's own named
    ``[PASS|FAIL] <check>`` lines (one ``--format all --verbose`` run) rather
    than by the overall exit code: the exit code ANDs every check together
    (including the subject-binding check, which runs unconditionally), so a
    bad subject digest would otherwise make every requirement -- including
    ones about formats that are perfectly fine -- look violated. A receipt
    whose only fault is (say) a mismatched COSE payload is reported with only
    the COSE requirements failing; the others still say PASS.

    A named check still covers more than one requirement in one case (e.g.
    ``cose`` covers both AR-COSE-001, signature validity, and AR-COSE-002,
    payload binding) -- the reference verifier does not report those two
    properties separately, so both are named together when either fails.
    """
    receipt_path = tmp_dir / "receipt-under-test.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    checks = _run_verifier_verbose(verifier_path, receipt_path)
    results: list[CaseResult] = []
    for req in REQUIREMENTS:
        check_name = _CHECK_NAME[req.applies_to]
        if check_name in checks:
            ok, detail = checks[check_name]
        else:
            ok, detail = False, f"verifier produced no {check_name!r} result (crash, timeout, or unknown format)"
        results.append(CaseResult("receipt-under-test", req.id, True, ok, detail))
    return tuple(results)
