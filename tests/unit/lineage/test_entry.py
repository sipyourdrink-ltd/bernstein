"""Tests for lineage entry schema + RFC 8785 JCS canonicalisation."""

from __future__ import annotations

import pytest

from bernstein.core.lineage.entry import (
    ACTIVITY_SOURCES,
    ARTEFACT_KINDS,
    LINEAGE_ENTRY_VERSION,
    LineageEntry,
    ModelRef,
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


def test_activity_source_defaults_to_none() -> None:
    entry = LineageEntry(**_kwargs())
    assert entry.activity_source is None


def test_activity_source_accepts_closed_set() -> None:
    assert frozenset({"scheduler", "adapter"}) == ACTIVITY_SOURCES
    for value in ACTIVITY_SOURCES:
        entry = LineageEntry(**_kwargs(activity_source=value))
        assert entry.activity_source == value


def test_activity_source_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="unknown activity_source"):
        LineageEntry(**_kwargs(activity_source="external"))


def test_canonicalise_drops_activity_source_when_none() -> None:
    # Issue #4962: pre-existing entries (activity_source unset) must keep
    # byte-identical wire bytes, so the canonicaliser drops the field.
    e_none = LineageEntry(**_kwargs())
    e_explicit_none = LineageEntry(**_kwargs(activity_source=None))
    assert canonicalise(e_none) == canonicalise(e_explicit_none)
    assert b"activity_source" not in canonicalise(e_none)


def test_canonicalise_keeps_activity_source_when_set() -> None:
    e = LineageEntry(**_kwargs(activity_source="scheduler"))
    body = canonicalise(e)
    assert b"activity_source" in body
    assert b"scheduler" in body


def test_entry_hash_changes_when_activity_source_changes() -> None:
    e1 = LineageEntry(**_kwargs())
    e2 = LineageEntry(**_kwargs(activity_source="scheduler"))
    assert entry_hash(e1) != entry_hash(e2)


# --- ModelRef tests (issue #5037) ---


def test_model_ref_required_fields() -> None:
    ref = ModelRef(provider="openai", model_requested="gpt-4o")
    assert ref.provider == "openai"
    assert ref.model_requested == "gpt-4o"
    assert ref.model_reported is None
    assert ref.version is None
    assert ref.routing_decision_hash == ""


def test_model_ref_all_fields() -> None:
    ref = ModelRef(
        provider="anthropic",
        model_requested="claude-3-opus",
        model_reported="claude-3-opus-20240229",
        version="opus-20240229",
        routing_decision_hash="sha256:" + "a" * 64,
    )
    assert ref.provider == "anthropic"
    assert ref.model_reported == "claude-3-opus-20240229"
    assert ref.version == "opus-20240229"


def test_model_ref_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        ModelRef(provider="", model_requested="gpt-4o")


def test_model_ref_rejects_empty_model_requested() -> None:
    with pytest.raises(ValueError, match="model_requested must be a non-empty string"):
        ModelRef(provider="openai", model_requested="")


def test_model_ref_rejects_empty_model_reported() -> None:
    with pytest.raises(ValueError, match="model_reported must be a non-empty string"):
        ModelRef(provider="openai", model_requested="gpt-4o", model_reported="")


def test_model_ref_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="version must be a non-empty string"):
        ModelRef(provider="openai", model_requested="gpt-4o", version="")


def test_model_ref_rejects_bad_routing_decision_hash_prefix() -> None:
    with pytest.raises(ValueError, match="routing_decision_hash must start with 'sha256:'"):
        ModelRef(
            provider="openai",
            model_requested="gpt-4o",
            routing_decision_hash="md5:deadbeef",
        )


def test_model_ref_accepts_all_closed_providers() -> None:
    from bernstein.core.lineage.entry import MODEL_REF_PROVIDERS

    for provider in MODEL_REF_PROVIDERS:
        ref = ModelRef(provider=provider, model_requested="test-model")
        assert ref.provider == provider


def test_model_ref_none_model_reported_is_valid() -> None:
    ref = ModelRef(provider="ollama", model_requested="llama-3", model_reported=None)
    assert ref.model_reported is None


def test_model_ref_none_version_is_valid() -> None:
    ref = ModelRef(provider="ollama", model_requested="llama-3", version=None)
    assert ref.version is None


def test_model_ref_empty_routing_decision_hash_is_valid() -> None:
    ref = ModelRef(provider="openai", model_requested="gpt-4o", routing_decision_hash="")
    assert ref.routing_decision_hash == ""
