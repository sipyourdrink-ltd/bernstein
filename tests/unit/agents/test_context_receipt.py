"""Tests for :mod:`bernstein.core.agents.context_receipt`."""

from __future__ import annotations

import hashlib

from bernstein.core.agents.context_receipt import (
    ContextReceipt,
    ContextReceiptEntry,
    build_context_receipt,
)


def test_entry_fields_match_spec() -> None:
    entry = ContextReceiptEntry(
        label="role",
        content_sha256="abc",
        token_estimate=10,
        char_count=100,
    )
    assert entry.label == "role"
    assert entry.content_sha256 == "abc"
    assert entry.token_estimate == 10
    assert entry.char_count == 100


def test_build_computes_correct_sha256() -> None:
    content = "You are a backend engineer."
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    receipt = build_context_receipt([("role", content)])
    assert receipt.entries[0].content_sha256 == expected


def test_build_computes_char_count() -> None:
    content = "hello world"
    receipt = build_context_receipt([("tasks", content)])
    assert receipt.entries[0].char_count == len(content)


def test_total_token_estimate_is_sum() -> None:
    receipt = build_context_receipt([("role", "aaa"), ("tasks", "bbbb"), ("lessons", "ccccc")])
    assert receipt.total_token_estimate == sum(e.token_estimate for e in receipt.entries)


def test_total_chars_is_sum() -> None:
    receipt = build_context_receipt([("role", "aaa"), ("tasks", "bbbb"), ("lessons", "ccccc")])
    assert receipt.total_chars == sum(e.char_count for e in receipt.entries)


def test_section_count_matches_entries() -> None:
    receipt = build_context_receipt([("a", "x"), ("b", "y")])
    assert receipt.section_count == 2
    assert len(receipt.entries) == 2


def test_empty_sections() -> None:
    receipt = build_context_receipt([])
    assert receipt.entries == []
    assert receipt.total_token_estimate == 0
    assert receipt.total_chars == 0
    assert receipt.section_count == 0


def test_deterministic_hashes() -> None:
    sections = [("role", "same content"), ("tasks", "also same")]
    r1 = build_context_receipt(sections)
    r2 = build_context_receipt(sections)
    assert r1 == r2


def test_different_content_different_hash() -> None:
    r1 = build_context_receipt([("role", "content A")])
    r2 = build_context_receipt([("role", "content B")])
    assert r1.entries[0].content_sha256 != r2.entries[0].content_sha256


def test_entry_to_dict_from_dict_round_trip() -> None:
    entry = ContextReceiptEntry(
        label="role",
        content_sha256="deadbeef",
        token_estimate=42,
        char_count=500,
    )
    d = entry.to_dict()
    assert d == {
        "label": "role",
        "content_sha256": "deadbeef",
        "token_estimate": 42,
        "char_count": 500,
    }
    assert ContextReceiptEntry.from_dict(d) == entry


def test_receipt_to_dict_from_dict_round_trip() -> None:
    receipt = build_context_receipt([("role", "You are helpful."), ("tasks", "Do the thing.")])
    d = receipt.to_dict()
    restored = ContextReceipt.from_dict(d)
    assert restored == receipt


def test_total_tokens_property_alias() -> None:
    receipt = build_context_receipt([("role", "some text")])
    assert receipt.total_tokens == receipt.total_token_estimate


def test_entry_order_preserved() -> None:
    receipt = build_context_receipt([("zeta", "zzz"), ("alpha", "aaa"), ("mid", "mmm")])
    assert [e.label for e in receipt.entries] == ["zeta", "alpha", "mid"]
