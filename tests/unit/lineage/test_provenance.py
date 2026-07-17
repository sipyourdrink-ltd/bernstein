"""Tests for provenance trust classes + deterministic taint projection.

The trust class of a tool result is recorded as a signed lineage entry; the
effective trust of any artefact is the minimum trust class over its lineage
closure -- a pure function of the log, recomputable offline. These tests pin
that projection, the fail-closed default, and the additive entry schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.lineage.entry import (
    LineageEntry,
    canonicalise,
    compute_operator_hmac,
    entry_hash,
)
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.provenance import (
    PROVENANCE_ARTEFACT_KIND,
    TaintVerdict,
    TrustClass,
    effective_trust,
    is_untrusted,
    load_trust_source_map,
    min_trust_class,
    record_tool_result,
    taint_for_artefact,
    trust_class_for_source,
    trust_rank,
)
from bernstein.core.lineage.recorder import LineageRecorder
from bernstein.core.lineage.store import LineageStore

# ---------------------------------------------------------------------------
# TrustClass ordering
# ---------------------------------------------------------------------------


def test_trust_rank_total_order() -> None:
    ranks = [trust_rank(tc) for tc in TrustClass]
    assert len(set(ranks)) == len(TrustClass)  # every class distinct
    assert trust_rank(TrustClass.OPERATOR) > trust_rank(TrustClass.WORKSPACE)
    assert trust_rank(TrustClass.WORKSPACE) > trust_rank(TrustClass.FIRST_PARTY)
    assert trust_rank(TrustClass.FIRST_PARTY) > trust_rank(TrustClass.THIRD_PARTY)
    assert trust_rank(TrustClass.THIRD_PARTY) > trust_rank(TrustClass.PUBLIC)


def test_min_trust_class_picks_least_trusted() -> None:
    assert min_trust_class(TrustClass.OPERATOR, TrustClass.PUBLIC) is TrustClass.PUBLIC
    assert min_trust_class(TrustClass.WORKSPACE, TrustClass.FIRST_PARTY) is TrustClass.FIRST_PARTY
    assert min_trust_class(TrustClass.THIRD_PARTY, TrustClass.THIRD_PARTY) is TrustClass.THIRD_PARTY


def test_is_untrusted_threshold() -> None:
    # Outsider-writable classes are untrusted; operator/workspace/first-party are not.
    assert is_untrusted(TrustClass.PUBLIC) is True
    assert is_untrusted(TrustClass.THIRD_PARTY) is True
    assert is_untrusted(TrustClass.FIRST_PARTY) is False
    assert is_untrusted(TrustClass.WORKSPACE) is False
    assert is_untrusted(TrustClass.OPERATOR) is False


# ---------------------------------------------------------------------------
# Additive entry schema
# ---------------------------------------------------------------------------


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        v=1,
        artefact_path="src/foo.py",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id="agent:worker",
        agent_card_kid="key-001",
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=1,
        operator_hmac="00" * 8,
    )
    base.update(overrides)
    return base


def test_trust_class_field_is_optional_and_backward_compatible() -> None:
    # An entry without trust_class canonicalises byte-identically to the
    # pre-feature form (the key is dropped, not serialised as null) so every
    # historical signature and HMAC stays valid.
    without = LineageEntry(**_kwargs())
    assert b"trust_class" not in canonicalise(without)


def test_trust_class_field_included_when_set() -> None:
    with_tc = LineageEntry(**_kwargs(artefact_kind=PROVENANCE_ARTEFACT_KIND, trust_class="third_party"))
    body = canonicalise(with_tc)
    assert b'"trust_class":"third_party"' in body
    # And it participates in the identity hash.
    without = LineageEntry(**_kwargs(artefact_kind=PROVENANCE_ARTEFACT_KIND))
    assert entry_hash(with_tc) != entry_hash(without)


def test_trust_class_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="trust_class"):
        LineageEntry(**_kwargs(trust_class="megatrust"))


def test_operator_hmac_covers_trust_class() -> None:
    key = b"k" * 32
    base = LineageEntry(**_kwargs(artefact_kind=PROVENANCE_ARTEFACT_KIND, trust_class="operator"))
    tampered = LineageEntry(**_kwargs(artefact_kind=PROVENANCE_ARTEFACT_KIND, trust_class="public"))
    assert compute_operator_hmac(base, key) != compute_operator_hmac(tampered, key)


# ---------------------------------------------------------------------------
# Deterministic taint projection
# ---------------------------------------------------------------------------


def _prov_entry(
    content_hash: str,
    parents: list[str],
    trust: str,
    *,
    path: str = "provenance/tool/x",
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind=PROVENANCE_ARTEFACT_KIND,
        content_hash=content_hash,
        parent_hashes=parents,
        agent_id="agent:gw",
        agent_card_kid="k",
        tool_call_id="tc",
        span_id="s",
        ts_ns=1,
        operator_hmac="",
        trust_class=trust,
    )


def _file_entry(content_hash: str, parents: list[str], *, path: str = "src/derived.py") -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parents,
        agent_id="agent:gw",
        agent_card_kid="k",
        tool_call_id="tc",
        span_id="s",
        ts_ns=2,
        operator_hmac="",
    )


def test_effective_trust_of_single_record_is_its_class() -> None:
    e = _prov_entry("sha256:" + "1" * 64, [], "third_party")
    verdict = effective_trust(entry_hash(e), [e])
    assert isinstance(verdict, TaintVerdict)
    assert verdict.trust is TrustClass.THIRD_PARTY
    assert verdict.tainted is True
    assert verdict.resolved is True


def test_effective_trust_is_minimum_over_closure() -> None:
    # A derived file whose closure unions an operator source and a public
    # source is as untrusted as the public source.
    op = _prov_entry("sha256:" + "1" * 64, [], "operator", path="provenance/tool/op")
    pub = _prov_entry("sha256:" + "2" * 64, [], "public", path="provenance/tool/pub")
    derived = _file_entry("sha256:" + "3" * 64, [entry_hash(op), entry_hash(pub)])
    verdict = effective_trust(entry_hash(derived), [op, pub, derived])
    assert verdict.trust is TrustClass.PUBLIC
    assert verdict.tainted is True


def test_effective_trust_propagates_through_multiple_hops() -> None:
    src = _prov_entry("sha256:" + "1" * 64, [], "third_party", path="provenance/tool/src")
    mid = _file_entry("sha256:" + "2" * 64, [entry_hash(src)], path="src/mid.py")
    leaf = _file_entry("sha256:" + "3" * 64, [entry_hash(mid)], path="src/leaf.py")
    verdict = effective_trust(entry_hash(leaf), [src, mid, leaf])
    assert verdict.trust is TrustClass.THIRD_PARTY
    assert verdict.tainted is True


def test_missing_provenance_fails_closed_to_lowest_trust() -> None:
    # An artefact whose target hash is not in the log at all.
    verdict = effective_trust("sha256:" + "f" * 64, [])
    assert verdict.resolved is False
    assert verdict.trust is TrustClass.PUBLIC
    assert verdict.tainted is True


def test_closure_without_any_trust_record_fails_closed() -> None:
    # A file with no provenance anywhere in its closure is lowest trust.
    lone = _file_entry("sha256:" + "9" * 64, [], path="src/lone.py")
    verdict = effective_trust(entry_hash(lone), [lone])
    assert verdict.trust is TrustClass.PUBLIC
    assert verdict.tainted is True


def test_verdict_is_byte_identical_across_independent_recomputes() -> None:
    op = _prov_entry("sha256:" + "1" * 64, [], "operator", path="provenance/tool/op")
    pub = _prov_entry("sha256:" + "2" * 64, [], "public", path="provenance/tool/pub")
    derived = _file_entry("sha256:" + "3" * 64, [entry_hash(op), entry_hash(pub)])
    entries = [op, pub, derived]
    target = entry_hash(derived)
    v1 = effective_trust(target, entries)
    # Shuffle the input order: the projection must not depend on log order.
    v2 = effective_trust(target, list(reversed(entries)))
    assert v1 == v2
    assert v1.closure == v2.closure  # deterministic sorted closure


def test_operator_source_is_not_tainted() -> None:
    op = _prov_entry("sha256:" + "1" * 64, [], "operator")
    verdict = effective_trust(entry_hash(op), [op])
    assert verdict.trust is TrustClass.OPERATOR
    assert verdict.tainted is False


# ---------------------------------------------------------------------------
# Source-to-trust-class map (reviewed data, fail-closed default)
# ---------------------------------------------------------------------------


def test_bundled_trust_source_map_loads() -> None:
    mapping = load_trust_source_map()
    # Outsider-writable surfaces are classified as untrusted.
    assert mapping["web.fetch"] is TrustClass.PUBLIC
    assert mapping["github.fetch_issue"] is TrustClass.THIRD_PARTY
    # Operator / workspace surfaces are trusted.
    assert is_untrusted(mapping["web.fetch"]) is True


def test_unknown_source_fails_closed_to_lowest_trust() -> None:
    mapping = {"web.fetch": TrustClass.PUBLIC}
    # An unlisted source is treated as the lowest trust class.
    assert trust_class_for_source("totally.unknown.tool", mapping) is TrustClass.PUBLIC
    assert trust_class_for_source("web.fetch", mapping) is TrustClass.PUBLIC


def test_default_map_used_when_none_passed() -> None:
    assert trust_class_for_source("github.fetch_issue") is TrustClass.THIRD_PARTY


# ---------------------------------------------------------------------------
# record_tool_result via the recorder (killer shape: the label IS a lineage record)
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder(tmp_path: Path) -> LineageRecorder:
    return LineageRecorder(store=LineageStore(tmp_path / "lineage"), operator_hmac_key=b"0" * 64)


@pytest.fixture
def card_and_key() -> tuple[AgentCard, str]:
    priv, pub = generate_keypair()
    return AgentCard(agent_id="agent:gw", kid="k1", public_key_pem=pub), priv


def test_record_tool_result_writes_a_provenance_lineage_entry(
    recorder: LineageRecorder,
    card_and_key: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_key
    h = record_tool_result(
        recorder,
        tool_name="web.fetch",
        result_bytes=b"<html>hostile page</html>",
        trust_class=TrustClass.PUBLIC,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    entries = [e for e, _ in recorder.store.read_log()]
    assert len(entries) == 1
    rec = entries[0]
    assert rec.artefact_kind == PROVENANCE_ARTEFACT_KIND
    assert rec.trust_class == "public"
    # The record is content-addressed by the tool-result bytes.
    import hashlib

    assert rec.content_hash == "sha256:" + hashlib.sha256(b"<html>hostile page</html>").hexdigest()
    # And the taint verdict resolves from the persisted log alone.
    verdict = effective_trust(h, entries)
    assert verdict.trust is TrustClass.PUBLIC
    assert verdict.tainted is True


def test_taint_for_artefact_resolves_latest_tip(
    recorder: LineageRecorder,
    card_and_key: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_key
    prov = record_tool_result(
        recorder,
        tool_name="github.fetch_issue",
        result_bytes=b'{"number": 5}',
        trust_class=TrustClass.THIRD_PARTY,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    # A derived file that names the provenance record as a lineage parent.
    recorder.record_write(
        artefact_path="src/from_issue.py",
        new_content=b"print('derived')",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-2",
        span_id="span-2",
        extra_parents=[prov],
    )
    entries = [e for e, _ in recorder.store.read_log()]
    verdict = taint_for_artefact("src/from_issue.py", entries)
    assert verdict.trust is TrustClass.THIRD_PARTY
    assert verdict.tainted is True
