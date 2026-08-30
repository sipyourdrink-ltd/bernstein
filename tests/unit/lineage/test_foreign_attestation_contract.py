"""Contract fixture for foreign attestations outside Bernstein's own chain.

The fixture carries the foreign envelope opaquely, by reference: only a format
tag, a payload hash and an unreadable signature blob, so nothing below depends
on which attestation format an issuer happens to use.

It pins the negative case first.  When a foreign issuer cannot be independently
verified, a future verifier must report ``unverifiable``.  That verdict is
distinct from Bernstein's own verified lineage, and equally distinct from a
signature that was evaluated and rejected.  "We could not evaluate this" and
"we evaluated this and it failed" are different outcomes, and reporting the
second in place of the first states a verdict on work that was never performed.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures") / "foreign_attestation_unverifiable.json"

#: Every key a foreign attestation may carry.  An allowlist rather than a
#: denylist of one known-bad name: a field smuggling our own chain material
#: into a foreign envelope has to fail here whatever it ends up being called.
_ALLOWED_ATTESTATION_KEYS = frozenset(
    {"issuer", "issuer_key_id", "content_hash", "claimed_subject", "trust_class", "envelope"}
)

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _key_paths(node: object, prefix: str = "") -> list[str]:
    """Return every mapping key under *node*, recursively, as dotted paths."""
    paths: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(_key_paths(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            paths.extend(_key_paths(value, f"{prefix}[{index}]"))
    return paths


def test_foreign_attestation_fixture_is_protocol_neutral_and_unlinked() -> None:
    fixture = _fixture()
    record = fixture["lineage_record"]
    assert isinstance(record, dict)
    attestation = record["external_attestation"]
    assert isinstance(attestation, dict)
    expected = fixture["expected"]
    assert isinstance(expected, dict)

    assert fixture["schema"] == "bernstein.foreign-attestation-fixture/v1"
    assert set(attestation) == _ALLOWED_ATTESTATION_KEYS
    assert attestation["trust_class"] == "third_party"
    assert _SHA256_DIGEST.fullmatch(str(attestation["content_hash"])) is not None

    # The fields a verifier has to resolve before it can reach a verdict at
    # all. Membership in the allowlist only proves the key is present, and an
    # empty issuer would make this fixture a different case: "there was
    # nothing here to check" rather than "this issuer cannot be checked".
    for field in ("issuer", "issuer_key_id", "claimed_subject"):
        value = attestation[field]
        assert isinstance(value, str) and value, f"{field} must be a usable string"

    # Never HMAC-chain evidence: no field anywhere beneath the record may carry
    # our own chain material, however it is named or however deeply nested.
    assert [path for path in _key_paths(record) if "hmac" in path.lower()] == []

    assert expected["foreign_attestation_is_not_hmac_chain_evidence"] is True
    assert expected["foreign_attestation_verdict"] == "unverifiable"
    assert expected["foreign_attestation_must_not_pass"] is True
    assert expected["derived_taint"] == "third_party"


def test_unverifiable_foreign_attestation_fails_closed_without_changing_local_chain() -> None:
    """A valid but unknown foreign format remains explicitly unverifiable."""
    from bernstein.core.lineage.foreign_attestation import verify_foreign_attestation

    fixture = _fixture()
    record = fixture["lineage_record"]
    assert isinstance(record, dict)
    attestation = record["external_attestation"]
    assert isinstance(attestation, dict)
    record_before = copy.deepcopy(record)

    result = verify_foreign_attestation(attestation)

    assert result.verdict == "unverifiable"
    assert result.verified is False
    assert result.taint.value == "third_party"

    # Judging the foreign claim must not write back into our own material, and
    # our own chain verdict stands on Bernstein lineage alone.
    assert record == record_before
    assert fixture["local_chain"] == {
        "expected_verdict": "verified",
        "evidence_source": "bernstein-lineage-only",
    }


def test_malformed_foreign_attestation_fails_closed_at_public_trust() -> None:
    from bernstein.core.lineage.foreign_attestation import verify_foreign_attestation

    fixture = _fixture()
    record = fixture["lineage_record"]
    assert isinstance(record, dict)
    original = record["external_attestation"]
    assert isinstance(original, dict)
    malformed = {**original, "trust_class": "operator_hmac"}

    result = verify_foreign_attestation(malformed)

    assert result.verdict == "malformed"
    assert result.verified is False
    assert result.taint.value == "public"


def test_unknown_envelope_format_is_unverifiable_not_a_parse_error() -> None:
    from bernstein.core.lineage.foreign_attestation import verify_foreign_attestation

    fixture = _fixture()
    record = fixture["lineage_record"]
    assert isinstance(record, dict)
    original = record["external_attestation"]
    assert isinstance(original, dict)
    envelope = original["envelope"]
    assert isinstance(envelope, dict)
    unknown_format = {**original, "envelope": {**envelope, "format": "future-foreign-format-v9"}}

    result = verify_foreign_attestation(unknown_format)

    assert result.verdict == "unverifiable"
    assert result.verified is False
    assert result.taint.value == "third_party"
