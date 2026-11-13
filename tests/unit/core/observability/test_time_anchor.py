"""Tests for the opt-in Trust Record time-anchor client.

No test touches the network: every call goes through a fake ``post``
transport, and the records are the signed vectors in
``tests/fixtures/trust-record-vectors/``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.observability.time_anchor import (
    AnchorReceipt,
    TimeAnchorError,
    anchor_record,
    check_endpoint,
    record_sha256,
)

_VECTORS = Path(__file__).parents[3] / "fixtures" / "trust-record-vectors"

# sha256 of the RFC 8785 form of each vector, computed with an independent
# JavaScript canonicaliser (UTF-16 key sort + JSON.stringify), not with
# Bernstein's own ``canonicalize_jcs``.
_EXPECTED_SHA = {
    "aggregate-trust-record.json": "12da5615149621656ace38c2c0139f3fb134f06d42e709c089947142780788a4",
    "delegated-child-trust-record.json": "d231119fde81369c160e5ef8c16b80a3691d36f6705f8bc1cda80b91aa50ca81",
    "delegated-grandchild-trust-record.json": "f8f23b1672ad2b9c0c13d06f4ade3b904be8ea3832566124a1d868e50f7d64de",
    "delegated-parent-trust-record.json": "61dc2465e0435c251d0b334ef0e77a2c349d1d789aa83cc4c225cca1f2408c57",
    "single-execution-trust-record.json": "62e4a01503fb745974ffb550836882d3c017ad7c17395442de848150445ba672",
    "supplementary-plane-child-trust-record.json": "88d3fc546690931133684effd2c0768f6a4fb6a4ab21482f11a9e21b9f540ef2",
    "supplementary-plane-parent-trust-record.json": "9ae1b732eac4b03e3d9cba08fa511e1928af4701371ccb1b4ba3be79dfae5293",
}

_ENDPOINT = "https://anchor.example.test/evidence/trace"


def _vector(name: str) -> str:
    return (_VECTORS / name).read_text(encoding="utf-8")


class _FakeAnchor:
    """Records what was posted and answers like a conforming anchor service."""

    def __init__(self, *, status: int = 201, answer: dict[str, Any] | None = None, raw: bytes | None = None) -> None:
        self.calls: list[tuple[str, bytes, float]] = []
        self._status = status
        self._answer = answer
        self._raw = raw

    def __call__(self, url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, body, timeout))
        if self._raw is not None:
            return self._status, self._raw
        if self._answer is not None:
            return self._status, json.dumps(self._answer).encode("utf-8")
        from bernstein.core.security.agent_card_signer import canonicalize_jcs

        record = json.loads(body)["record"]
        sha = hashlib.sha256(canonicalize_jcs(record)).hexdigest()
        return self._status, json.dumps({"sha": sha, "status": "pending", "url": f"/x/{sha}"}).encode("utf-8")


@pytest.mark.parametrize("name", sorted(_EXPECTED_SHA))
def test_record_sha256_matches_independent_jcs(name: str) -> None:
    assert record_sha256(_vector(name)) == _EXPECTED_SHA[name]


@pytest.mark.parametrize("family", ["delegated", "supplementary-plane"])
def test_anchor_digest_is_the_trace_parent_record_hash(family: str) -> None:
    """The anchored sha is the identifier a child hop already uses for its parent."""
    parent = _vector(f"{family}-parent-trust-record.json")
    child = json.loads(_vector(f"{family}-child-trust-record.json"))
    assert child["delegation"]["parent_record_hash"] == "sha256:" + record_sha256(parent)


def test_record_sha256_uses_utf16_key_order_not_code_points() -> None:
    raw = _vector("supplementary-plane-parent-trust-record.json")
    by_code_point = hashlib.sha256(
        json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert record_sha256(raw) != by_code_point
    # The file is stored with \\u escapes, so its bytes are not the JCS form either.
    assert record_sha256(raw) != hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def test_anchor_record_posts_record_and_returns_checked_receipt() -> None:
    raw = _vector("single-execution-trust-record.json")
    fake = _FakeAnchor()
    receipt = anchor_record(raw, _ENDPOINT, timeout=3.0, post=fake)

    assert isinstance(receipt, AnchorReceipt)
    assert receipt.sha256 == _EXPECTED_SHA["single-execution-trust-record.json"]
    assert receipt.status == "pending"
    assert receipt.endpoint == _ENDPOINT
    assert receipt.raw["url"].endswith(receipt.sha256)
    [(url, body, timeout)] = fake.calls
    assert (url, timeout) == (_ENDPOINT, 3.0)
    assert json.loads(body) == {"record": json.loads(raw)}

    saved = json.loads(receipt.to_json())
    assert saved["record_sha256"] == receipt.sha256
    assert saved["response"] == receipt.raw


def test_anchor_record_accepts_anchored_status() -> None:
    raw = _vector("aggregate-trust-record.json")
    sha = _EXPECTED_SHA["aggregate-trust-record.json"]
    receipt = anchor_record(raw, _ENDPOINT, post=_FakeAnchor(status=200, answer={"sha": sha, "status": "anchored"}))
    assert receipt.status == "anchored"


def test_anchor_record_rejects_receipt_for_other_bytes() -> None:
    raw = _vector("single-execution-trust-record.json")
    other = _EXPECTED_SHA["aggregate-trust-record.json"]
    with pytest.raises(TimeAnchorError, match="receipt names sha"):
        anchor_record(raw, _ENDPOINT, post=_FakeAnchor(answer={"sha": other, "status": "pending"}))


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "pending"},
        {"sha": "ABC", "status": "pending"},
        {"sha": "62E4A01503FB745974FFB550836882D3C017AD7C17395442DE848150445BA672", "status": "pending"},
    ],
)
def test_anchor_record_rejects_missing_or_malformed_sha(answer: dict[str, Any]) -> None:
    raw = _vector("single-execution-trust-record.json")
    with pytest.raises(TimeAnchorError, match="64-hex"):
        anchor_record(raw, _ENDPOINT, post=_FakeAnchor(answer=answer))


def test_anchor_record_rejects_unknown_status() -> None:
    raw = _vector("single-execution-trust-record.json")
    sha = _EXPECTED_SHA["single-execution-trust-record.json"]
    with pytest.raises(TimeAnchorError, match="status"):
        anchor_record(raw, _ENDPOINT, post=_FakeAnchor(answer={"sha": sha, "status": "verified"}))


def test_anchor_record_reports_refusal_reason() -> None:
    raw = _vector("single-execution-trust-record.json")
    fake = _FakeAnchor(status=422, answer={"reason_code": "signature_invalid"})
    with pytest.raises(TimeAnchorError, match=r"HTTP 422 \(signature_invalid\)"):
        anchor_record(raw, _ENDPOINT, post=fake)


@pytest.mark.parametrize("raw_answer", [b"not json", b"[1, 2]"])
def test_anchor_record_rejects_non_object_answer(raw_answer: bytes) -> None:
    raw = _vector("single-execution-trust-record.json")
    with pytest.raises(TimeAnchorError):
        anchor_record(raw, _ENDPOINT, post=_FakeAnchor(raw=raw_answer))


@pytest.mark.parametrize(
    "endpoint",
    ["http://anchor.example.test/evidence/trace", "ftp://anchor.example.test/x", "file:///etc/passwd", "anchor"],
)
def test_endpoint_must_be_https(endpoint: str) -> None:
    fake = _FakeAnchor()
    with pytest.raises(TimeAnchorError):
        anchor_record(_vector("single-execution-trust-record.json"), endpoint, post=fake)
    assert fake.calls == []


def test_loopback_http_is_allowed_for_local_testing() -> None:
    assert check_endpoint("http://127.0.0.1:8787/evidence/trace") == "http://127.0.0.1:8787/evidence/trace"


@pytest.mark.parametrize("record_json", ["{not json", "[1, 2, 3]"])
def test_record_must_be_a_json_object(record_json: str) -> None:
    fake = _FakeAnchor()
    with pytest.raises(TimeAnchorError):
        anchor_record(record_json, _ENDPOINT, post=fake)
    assert fake.calls == []
