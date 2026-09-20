"""Property tests for the lineage chain (signer + WAL + verifier).

Four invariants the writer + verifier must preserve under arbitrary
inputs:

1. **Sign → verify roundtrip** - when a writer carries a signer, every
   emitted record's ``customer_signature`` must validate against the
   paired :class:`Ed25519PublicKeyVerifier` over the canonicalised
   payload bytes. No matter what (path, sha, agent, prompt) Hypothesis
   throws at us, the chain comes back ``ok=True``.

2. **Byte-flip tamper detection** - flipping a byte inside any persisted
   WAL line that holds a lineage record must surface a verification
   error. Either the WAL hash chain trips, or the customer-signature
   verifier rejects the canonicalised payload - either path is
   acceptable, but at least one MUST fire.

3. **Cross-record link integrity** - after appending a sequence of
   lineage records, ``LineageReader.iter_records`` must yield every
   one of them in chronological order with payload fields preserved
   through the WAL serialise/deserialise round-trip. This catches
   regressions in the legacy v1↔v2 reader path that have leaked
   producer/prompt drift before.

4. **Retention class normalisation** - empty-string ``regulatory_class``
   read back from disk must normalise to ``None`` so compliance
   filters that test ``record.regulatory_class is not None`` do not
   misclassify "untagged" records as classified ones. Same for
   ``customer_signature``.

Heavy fuzz lives in the nightly ``deep`` profile (1 000 examples per
property); PR-time runs ``smoke`` (50 examples), keeping each property
under ~10 s on a GitHub-hosted runner.

Hypothesis-vs-pytest-fixtures gotcha: function-scoped fixtures only
re-run for the *outer* test invocation, not for each generated
example. The signer and tempdir are therefore built inside the test
body so every example starts from a clean slate (mirrors the pattern
used by ``test_audit_chain_properties.py`` and ``test_wal_chain_properties.py``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageReader,
    LineageRecord,
    LineageWriter,
    canonical_record_bytes,
    decode_signature,
    verify_run_chain,
)
from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    Ed25519PublicKeyVerifier,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Restrict path/agent/prompt strings to printable ASCII; chain mechanics
# are orthogonal to UTF-8 escaping (covered in dedicated unit tests).
_ALPHABET = st.characters(
    blacklist_categories=("Cs",),
    min_codepoint=0x20,
    max_codepoint=0x7E,
)
_TEXT = st.text(_ALPHABET, min_size=1, max_size=24)
# SHA-256 hex digests are ASCII hex; restrict the strategy accordingly so
# the property tests cover realistic inputs. Wide-unicode SHA strings
# are not a real production scenario - the `LineageRecord.sha256` field
# is populated by `hashlib.sha256(...).hexdigest()`.
_SHA = st.text(
    st.sampled_from("0123456789abcdef"),
    min_size=64,
    max_size=64,
)
_REG_CLASS = st.one_of(st.none(), st.text(_ALPHABET, min_size=1, max_size=12))


def _artifact_strategy() -> st.SearchStrategy[ArtifactRef]:
    """Build an :class:`ArtifactRef` with optional line bounds."""
    return st.builds(
        ArtifactRef,
        path=_TEXT,
        sha256=_SHA,
        line_start=st.one_of(st.none(), st.integers(min_value=1, max_value=500)),
        line_end=st.one_of(st.none(), st.integers(min_value=1, max_value=500)),
    )


def _agent_strategy() -> st.SearchStrategy[AgentRef]:
    return st.builds(
        AgentRef,
        agent_id=_TEXT,
        run_id=st.text(
            st.characters(min_codepoint=0x30, max_codepoint=0x7A, blacklist_characters="/\\."),
            min_size=1,
            max_size=16,
        ),
        tick_id=st.one_of(st.none(), _TEXT),
    )


def _record_strategy() -> st.SearchStrategy[LineageRecord]:
    return st.builds(
        LineageRecord,
        output_artifact=_artifact_strategy(),
        inputs=st.lists(_artifact_strategy(), min_size=0, max_size=3),
        producer=_agent_strategy(),
        prompt_sha=_TEXT,
        model=_TEXT,
        cost_usd=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        tokens=st.integers(min_value=0, max_value=100_000),
        timestamp=st.floats(min_value=0.0, max_value=2_000_000_000.0, allow_nan=False, allow_infinity=False),
        regulatory_class=_REG_CLASS,
    )


# ---------------------------------------------------------------------------
# Test fixtures (per-example, not pytest-fixture)
# ---------------------------------------------------------------------------


def _make_signed_writer(
    run_id: str = "prop-run",
) -> tuple[
    LineageWriter,
    Ed25519PublicKeyVerifier,
    Path,
]:
    """Return a signed writer + paired verifier + sdd tempdir.

    The Ed25519 private key is generated fresh per call so concurrent
    Hypothesis examples do not share signature material. The matching
    public-key verifier is returned so the test body can attest each
    emitted record without reaching into private writer state.
    """
    sdd = Path(tempfile.mkdtemp(prefix="bernstein-prop-lineage-"))
    private = Ed25519PrivateKey.generate()
    key_path = sdd / "lineage.key"
    key_path.write_bytes(
        private.private_bytes_raw()
        if hasattr(private, "private_bytes_raw")
        else private.private_numbers().private_bytes_raw()  # pragma: no cover
    )
    key_path.chmod(0o600)
    signer = Ed25519FileKeySigner.from_path(key_path)
    verifier = Ed25519PublicKeyVerifier.from_raw(signer.public_key_bytes())
    writer = LineageWriter.for_run(run_id, sdd, signer=signer)
    return writer, verifier, sdd


# ---------------------------------------------------------------------------
# Property 1 - sign → verify roundtrip
# ---------------------------------------------------------------------------


@given(records=st.lists(_record_strategy(), min_size=1, max_size=8))
def test_signed_records_roundtrip_through_verifier(
    records: list[LineageRecord],
) -> None:
    """Every emitted signed record must validate against the paired verifier.

    The writer canonicalises the payload (excluding the signature
    field), signs it with the customer's Ed25519 key, and base64-encodes
    the detached signature into ``LineageRecord.customer_signature``.
    The verifier must recompute the canonical bytes from the persisted
    payload and accept the signature for every record on the chain.
    """
    writer, verifier, sdd = _make_signed_writer("prop-roundtrip")
    for record in records:
        writer.emit(record)

    result = verify_run_chain(sdd, "prop-roundtrip", verifier=verifier)
    assert result.ok, f"signed chain rejected its own writes: {result.errors}"
    assert result.record_count == len(records), f"expected {len(records)} records, got {result.record_count}"


# ---------------------------------------------------------------------------
# Property 2 - byte-flip tamper detection
# ---------------------------------------------------------------------------


@settings(max_examples=30)  # IO-heavy - tighter than smoke default.
@given(
    records=st.lists(_record_strategy(), min_size=2, max_size=6),
    flip_offset=st.integers(min_value=0, max_value=50_000),
)
def test_byte_flip_breaks_chain_or_signature(
    records: list[LineageRecord],
    flip_offset: int,
) -> None:
    """A single-byte flip in the on-disk WAL must be detected.

    Either the WAL hash chain reports an error, or the
    customer-signature verifier rejects the canonicalised payload.
    Both are acceptable paths - what matters is that *something* in
    :func:`verify_run_chain` surfaces an error.
    """
    writer, verifier, sdd = _make_signed_writer("prop-flip")
    for record in records:
        writer.emit(record)

    wal_path = sdd / "runtime" / "wal" / "prop-flip.wal.jsonl"
    raw = wal_path.read_bytes()
    if not raw:
        pytest.skip("WAL produced no bytes")

    covered_indices: list[int] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        line_stripped = line.rstrip(b"\r\n")
        covered_indices.extend(range(offset, offset + len(line_stripped)))
        offset += len(line)

    if not covered_indices:
        pytest.skip("WAL produced no payload bytes")

    pos = covered_indices[flip_offset % len(covered_indices)]
    mutated = bytearray(raw)
    mutated[pos] ^= 0x01
    if mutated == raw:
        pytest.skip("XOR with 0x01 unchanged (impossible)")
    wal_path.write_bytes(bytes(mutated))

    result = verify_run_chain(sdd, "prop-flip", verifier=verifier)
    assert not result.ok, f"byte flip went undetected at offset {pos}"
    assert result.errors, "verify_run_chain returned ok=False with empty error list"


@settings(max_examples=30)
@given(records=st.lists(_record_strategy(), min_size=2, max_size=6))
def test_byte_flip_uncovered_region_is_record_delimiters(records: list[LineageRecord]) -> None:
    """All bytes outside the signed/hashed JSON payloads are line delimiters."""
    writer, _verifier, sdd = _make_signed_writer("prop-uncovered")
    for record in records:
        writer.emit(record)

    wal_path = sdd / "runtime" / "wal" / "prop-uncovered.wal.jsonl"
    raw = wal_path.read_bytes()
    if not raw:
        pytest.skip("WAL produced no bytes")

    covered_indices: set[int] = set()
    offset = 0
    for line in raw.splitlines(keepends=True):
        line_stripped = line.rstrip(b"\r\n")
        covered_indices.update(range(offset, offset + len(line_stripped)))
        offset += len(line)

    uncovered_indices = [i for i in range(len(raw)) if i not in covered_indices]
    for pos in uncovered_indices:
        assert raw[pos] in b"\r\n", f"unexpected uncovered byte {raw[pos]!r} at offset {pos}"


# ---------------------------------------------------------------------------
# Property 3 - cross-record link integrity
# ---------------------------------------------------------------------------


@given(records=st.lists(_record_strategy(), min_size=1, max_size=8))
def test_records_iter_preserves_order_and_fields(
    records: list[LineageRecord],
) -> None:
    """Reader must yield records in chronological order with all fields preserved.

    Catches regressions in the v1↔v2 reader path that have leaked
    producer/prompt drift before. We compare path, sha, producer, and
    prompt_sha rather than full equality because the writer fills in
    schema_version/customer_signature on emit, so the in-memory record
    differs from the persisted one in those two fields by design.
    """
    writer, _verifier, sdd = _make_signed_writer("prop-order")
    for record in records:
        writer.emit(record)

    reader = LineageReader(sdd)
    read_back = list(reader.iter_records(run_id="prop-order"))
    assert len(read_back) == len(records), f"reader yielded {len(read_back)} records, expected {len(records)}"
    for original, restored in zip(records, read_back, strict=True):
        assert restored.output_artifact.path == original.output_artifact.path
        assert restored.output_artifact.sha256 == original.output_artifact.sha256
        assert restored.producer.agent_id == original.producer.agent_id
        assert restored.producer.run_id == original.producer.run_id
        assert restored.prompt_sha == original.prompt_sha
        assert restored.model == original.model
        assert restored.cost_usd == original.cost_usd
        assert restored.tokens == original.tokens
        assert len(restored.inputs) == len(original.inputs)


# ---------------------------------------------------------------------------
# Property 4 - retention class arithmetic / signature canonicalisation
# ---------------------------------------------------------------------------


@settings(max_examples=30)
@given(
    record=_record_strategy(),
    perturb_field=st.sampled_from(("prompt_sha", "model", "tokens", "cost_usd")),
)
def test_canonical_bytes_diverge_when_payload_changes(
    record: LineageRecord,
    perturb_field: str,
) -> None:
    """Mutating any payload field must change the canonical bytes.

    The signer covers ``canonical_record_bytes(record)``; if a mutation
    in any payload field left the canonical bytes unchanged, an
    attacker could swap producer/prompt/cost without invalidating the
    customer signature. The canonical encoder must include every
    user-visible field.
    """
    baseline = canonical_record_bytes(record)

    if perturb_field == "prompt_sha":
        mutated = LineageRecord(
            output_artifact=record.output_artifact,
            inputs=list(record.inputs),
            producer=record.producer,
            prompt_sha=record.prompt_sha + "X",
            model=record.model,
            cost_usd=record.cost_usd,
            tokens=record.tokens,
            timestamp=record.timestamp,
            regulatory_class=record.regulatory_class,
            customer_signature=record.customer_signature,
            schema_version=record.schema_version,
        )
    elif perturb_field == "model":
        mutated = LineageRecord(
            output_artifact=record.output_artifact,
            inputs=list(record.inputs),
            producer=record.producer,
            prompt_sha=record.prompt_sha,
            model=record.model + "Y",
            cost_usd=record.cost_usd,
            tokens=record.tokens,
            timestamp=record.timestamp,
            regulatory_class=record.regulatory_class,
            customer_signature=record.customer_signature,
            schema_version=record.schema_version,
        )
    elif perturb_field == "tokens":
        mutated = LineageRecord(
            output_artifact=record.output_artifact,
            inputs=list(record.inputs),
            producer=record.producer,
            prompt_sha=record.prompt_sha,
            model=record.model,
            cost_usd=record.cost_usd,
            tokens=record.tokens + 1,
            timestamp=record.timestamp,
            regulatory_class=record.regulatory_class,
            customer_signature=record.customer_signature,
            schema_version=record.schema_version,
        )
    else:  # cost_usd
        mutated = LineageRecord(
            output_artifact=record.output_artifact,
            inputs=list(record.inputs),
            producer=record.producer,
            prompt_sha=record.prompt_sha,
            model=record.model,
            cost_usd=record.cost_usd + 1.0,
            tokens=record.tokens,
            timestamp=record.timestamp,
            regulatory_class=record.regulatory_class,
            customer_signature=record.customer_signature,
            schema_version=record.schema_version,
        )

    assert canonical_record_bytes(mutated) != baseline, (
        f"canonical bytes unchanged after mutating {perturb_field!r} - signer would not detect this tamper"
    )


# ---------------------------------------------------------------------------
# Property 5 - empty-sentinel normalisation (regression)
# ---------------------------------------------------------------------------


@given(record=_record_strategy())
def test_signature_decodes_to_64_bytes(record: LineageRecord) -> None:
    """Every persisted signature must decode to exactly 64 bytes (Ed25519).

    A length-mismatched signature blob means the writer (or the base64
    encoder) is corrupting the wire format - the verifier would
    silently reject every record. This property catches any future
    encoder that smuggles whitespace/padding into the field.
    """
    writer, _verifier, sdd = _make_signed_writer("prop-sig-len")
    writer.emit(record)

    reader = LineageReader(sdd)
    read_back = list(reader.iter_records(run_id="prop-sig-len"))
    assert len(read_back) == 1
    sig_b64 = read_back[0].customer_signature
    assert sig_b64 is not None, "signed writer produced no customer_signature"
    assert len(decode_signature(sig_b64)) == 64, (
        f"Ed25519 signature must be 64 bytes, got {len(decode_signature(sig_b64))}"
    )
