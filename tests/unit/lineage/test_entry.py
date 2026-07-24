"""Tests for lineage entry schema + RFC 8785 JCS canonicalisation."""

from __future__ import annotations

import pytest

from bernstein.core.lineage.entry import (
    ARTEFACT_KINDS,
    LINEAGE_ENTRY_VERSION,
    LineageEntry,
    canonicalise,
    entry_hash,
)


def _kwargs(**overrides):
    base = dict(
        v=1,
        artefact_path="src/foo.py",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id="agent:claude-worker-3",
        agent_card_kid="key-001",
        tool_call_id="tc-7f3a",
        span_id="00f067aa0ba902b7",
        ts_ns=1_715_600_000_000_000_000,
        operator_hmac="deadbeef" * 8,
    )
    base.update(overrides)
    return base


def test_canonicalise_deterministic_across_constructions():
    e1 = LineageEntry(**_kwargs(parent_hashes=["sha256:" + "0" * 64]))
    e2 = LineageEntry(**_kwargs(parent_hashes=["sha256:" + "0" * 64]))
    assert canonicalise(e1) == canonicalise(e2)


def test_canonicalise_keys_sorted():
    e = LineageEntry(**_kwargs())
    b = canonicalise(e)
    # First sorted key in our schema is "agent_card_kid"
    assert b.startswith(b'{"agent_card_kid":')


def test_canonicalise_no_whitespace_or_bom():
    e = LineageEntry(**_kwargs())
    b = canonicalise(e)
    assert b"\n" not in b
    assert b": " not in b  # JCS forbids whitespace after colon
    assert b", " not in b
    assert b[:1] != b"\xef"  # no BOM


def test_entry_hash_format():
    e = LineageEntry(**_kwargs())
    h = entry_hash(e)
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_entry_hash_changes_with_content():
    e1 = LineageEntry(**_kwargs())
    e2 = LineageEntry(**_kwargs(ts_ns=e1.ts_ns + 1))
    assert entry_hash(e1) != entry_hash(e2)


def test_rejects_wrong_version():
    with pytest.raises(ValueError, match="unsupported entry version"):
        LineageEntry(**_kwargs(v=2))


def test_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown artefact_kind"):
        LineageEntry(**_kwargs(artefact_kind="bogus"))


def test_rejects_bad_content_hash_prefix():
    with pytest.raises(ValueError, match="content_hash must"):
        LineageEntry(**_kwargs(content_hash="md5:nope"))


def test_rejects_bad_parent_hash_prefix():
    with pytest.raises(ValueError, match="parent_hash must"):
        LineageEntry(**_kwargs(parent_hashes=["nope"]))


def test_accepts_all_artefact_kinds():
    for kind in ARTEFACT_KINDS:
        LineageEntry(**_kwargs(artefact_kind=kind))


def test_accepts_widened_non_coding_kinds():
    # Issue #2608: the non-coding artifact kinds must be recordable.
    for kind in ("report", "dataset", "action_log", "ops_result"):
        assert kind in ARTEFACT_KINDS
        entry = LineageEntry(**_kwargs(artefact_kind=kind))
        assert entry.artefact_kind == kind


def test_widening_keeps_the_set_closed():
    # A kind outside the closed set (including the coding-path ``code_diff``,
    # which is recorded as a ``file`` write, not its own lineage kind) raises.
    for bogus in ("code_diff", "screenshot", "unknown"):
        with pytest.raises(ValueError, match="unknown artefact_kind"):
            LineageEntry(**_kwargs(artefact_kind=bogus))


def test_version_constant():
    assert LINEAGE_ENTRY_VERSION == 1
