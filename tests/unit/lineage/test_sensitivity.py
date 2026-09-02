"""Tests for sensitivity classes + deterministic sensitivity projection.

The sensitivity of a source is recorded as a signed lineage entry; the
*effective* sensitivity of any artefact is the **maximum** sensitivity class
over its lineage closure -- the mirror of the trust projection in
:mod:`bernstein.core.lineage.provenance`, with the propagation rule inverted.
These tests pin that projection, the fail-closed-high default, the closure
member the verdict blames, and the additive entry schema.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.lineage.entry import (
    LineageEntry,
    canonicalise,
    compute_operator_hmac,
    entry_hash,
)
from bernstein.core.lineage.gate import check
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached
from bernstein.core.lineage.provenance import load_entries_from_log
from bernstein.core.lineage.sensitivity import (
    HIGHEST_SENSITIVITY_CLASS,
    SensitivityClass,
    SensitivityVerdict,
    effective_sensitivity,
    max_sensitivity_class,
    sensitivity_for_artefact,
    sensitivity_rank,
)

# ---------------------------------------------------------------------------
# SensitivityClass ordering
# ---------------------------------------------------------------------------


def test_sensitivity_rank_total_order() -> None:
    ranks = [sensitivity_rank(sc) for sc in SensitivityClass]
    assert len(set(ranks)) == len(SensitivityClass)  # every class distinct
    assert sensitivity_rank(SensitivityClass.RESTRICTED) > sensitivity_rank(SensitivityClass.CONFIDENTIAL)
    assert sensitivity_rank(SensitivityClass.CONFIDENTIAL) > sensitivity_rank(SensitivityClass.INTERNAL)
    assert sensitivity_rank(SensitivityClass.INTERNAL) > sensitivity_rank(SensitivityClass.PUBLIC)


def test_max_sensitivity_class_picks_the_more_sensitive() -> None:
    assert max_sensitivity_class(SensitivityClass.PUBLIC, SensitivityClass.RESTRICTED) is SensitivityClass.RESTRICTED
    assert (
        max_sensitivity_class(SensitivityClass.INTERNAL, SensitivityClass.CONFIDENTIAL) is SensitivityClass.CONFIDENTIAL
    )
    assert max_sensitivity_class(SensitivityClass.INTERNAL, SensitivityClass.INTERNAL) is SensitivityClass.INTERNAL


def test_highest_class_is_the_fail_closed_default() -> None:
    assert HIGHEST_SENSITIVITY_CLASS is SensitivityClass.RESTRICTED
    assert sensitivity_rank(HIGHEST_SENSITIVITY_CLASS) == max(sensitivity_rank(sc) for sc in SensitivityClass)


# ---------------------------------------------------------------------------
# 1. Additive entry schema (ADR-009 SS5.2)
# ---------------------------------------------------------------------------

#: Canonical bytes of an entry carrying no ``sensitivity``, captured from the
#: pre-change schema. An entry that records no sensitivity must serialise to
#: exactly these bytes, so every historical signature, HMAC and entry hash
#: stays valid.
_GOLDEN_PRE_CHANGE_CANONICAL = (
    b'{"agent_card_kid":"key-001","agent_id":"agent:worker","artefact_kind":"file",'
    b'"artefact_path":"docs/summary.md",'
    b'"content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"operator_hmac":"0000000000000000000000000000000000000000000000000000000000000000",'
    b'"parent_hashes":["sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],'
    b'"span_id":"span-1","tool_call_id":"tc-1","ts_ns":1700000000000000001,"v":1}'
)

_GOLDEN_PRE_CHANGE_ENTRY_HASH = "sha256:b05fdd36bee74e6b9cd5a502ac00145e333992830658f953bdd58049aa615eff"
_GOLDEN_PRE_CHANGE_HMAC = "0250aa0dd6a431d99c81973caaa91cdad6793612b00d1e0539cd3eaff03c1570"


def _golden_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        v=1,
        artefact_path="docs/summary.md",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=["sha256:" + "b" * 64],
        agent_id="agent:worker",
        agent_card_kid="key-001",
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=1_700_000_000_000_000_001,
        operator_hmac="00" * 32,
    )
    base.update(overrides)
    return base


def test_entry_without_sensitivity_canonicalises_byte_identically_to_the_pre_change_schema() -> None:
    # Test 1. The drop-when-None rule of ADR-009 SS5.2: an entry that carries no
    # sensitivity must produce the exact bytes the pre-change schema produced,
    # so historical signatures, HMACs and entry hashes are untouched.
    implicit = LineageEntry(**_golden_kwargs())
    explicit_none = LineageEntry(**_golden_kwargs(sensitivity=None))

    assert canonicalise(implicit) == _GOLDEN_PRE_CHANGE_CANONICAL
    assert canonicalise(explicit_none) == _GOLDEN_PRE_CHANGE_CANONICAL
    assert b"sensitivity" not in canonicalise(implicit)
    assert entry_hash(implicit) == _GOLDEN_PRE_CHANGE_ENTRY_HASH
    assert compute_operator_hmac(implicit, b"k" * 32) == _GOLDEN_PRE_CHANGE_HMAC


def test_sensitivity_is_included_in_the_canonical_bytes_when_set() -> None:
    labelled = LineageEntry(**_golden_kwargs(sensitivity="confidential"))
    body = canonicalise(labelled)
    assert b'"sensitivity":"confidential"' in body
    # And it participates in the identity hash, so a label cannot be swapped
    # without moving the entry hash.
    assert entry_hash(labelled) != _GOLDEN_PRE_CHANGE_ENTRY_HASH


def test_sensitivity_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="unknown sensitivity"):
        LineageEntry(**_golden_kwargs(sensitivity="top_secret"))


def test_operator_hmac_covers_sensitivity() -> None:
    key = b"k" * 32
    low = LineageEntry(**_golden_kwargs(sensitivity="internal"))
    high = LineageEntry(**_golden_kwargs(sensitivity="restricted"))
    assert compute_operator_hmac(low, key) != compute_operator_hmac(high, key)


def test_sensitivity_and_trust_class_are_independent_fields() -> None:
    # Opposite axes: a public web page can be low-trust and low-sensitivity;
    # an operator-supplied document is high-trust and may be high-sensitivity.
    both = LineageEntry(**_golden_kwargs(trust_class="operator", sensitivity="confidential"))
    body = canonicalise(both)
    assert b'"trust_class":"operator"' in body
    assert b'"sensitivity":"confidential"' in body


# ---------------------------------------------------------------------------
# Projection fixtures
# ---------------------------------------------------------------------------


def _entry(
    path: str,
    content_hash: str,
    parents: list[str],
    *,
    sensitivity: str | None = None,
    ts_ns: int = 1,
) -> LineageEntry:
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
        ts_ns=ts_ns,
        operator_hmac="",
        sensitivity=sensitivity,
    )


# ---------------------------------------------------------------------------
# 2. Maximum over the closure
# ---------------------------------------------------------------------------


def test_effective_sensitivity_is_the_maximum_over_the_closure() -> None:
    # Test 2. A derived artefact whose closure unions a public source and a
    # confidential source is as sensitive as the confidential source.
    pub = _entry("docs/public.md", "sha256:" + "1" * 64, [], sensitivity="public")
    conf = _entry("docs/secret.md", "sha256:" + "2" * 64, [], sensitivity="confidential")
    derived = _entry("docs/merged.md", "sha256:" + "3" * 64, [entry_hash(pub), entry_hash(conf)], ts_ns=2)

    verdict = effective_sensitivity(entry_hash(derived), [pub, conf, derived])

    assert isinstance(verdict, SensitivityVerdict)
    assert verdict.sensitivity is SensitivityClass.CONFIDENTIAL
    assert verdict.resolved is True


def test_effective_sensitivity_of_a_single_labelled_record_is_its_own_class() -> None:
    e = _entry("docs/secret.md", "sha256:" + "1" * 64, [], sensitivity="restricted")
    verdict = effective_sensitivity(entry_hash(e), [e])
    assert verdict.sensitivity is SensitivityClass.RESTRICTED
    assert verdict.resolved is True


# ---------------------------------------------------------------------------
# 3. Fail closed to the highest class
# ---------------------------------------------------------------------------


def test_absent_sensitivity_fails_closed_to_the_highest_class() -> None:
    # Test 3. Two shapes of absence, both fail closed *high* -- the mirror of
    # the taint projection failing closed *low*. An unlabelled artefact of
    # unknown origin is not assumed harmless.
    unknown = effective_sensitivity("sha256:" + "f" * 64, [])
    assert unknown.resolved is False
    assert unknown.sensitivity is SensitivityClass.RESTRICTED
    assert unknown.raised_by is None

    lone = _entry("docs/lone.md", "sha256:" + "9" * 64, [])
    unlabelled = effective_sensitivity(entry_hash(lone), [lone])
    assert unlabelled.resolved is True
    assert unlabelled.sensitivity is SensitivityClass.RESTRICTED
    assert unlabelled.raised_by is None


def test_a_public_label_anywhere_in_the_closure_is_not_absence() -> None:
    # An explicit ``public`` label is a classification, not a missing one, so
    # it must not be promoted to the fail-closed default.
    src = _entry("docs/public.md", "sha256:" + "1" * 64, [], sensitivity="public")
    derived = _entry("docs/copy.md", "sha256:" + "2" * 64, [entry_hash(src)], ts_ns=2)
    verdict = effective_sensitivity(entry_hash(derived), [src, derived])
    assert verdict.sensitivity is SensitivityClass.PUBLIC


# ---------------------------------------------------------------------------
# 4. The summary case (load-bearing)
# ---------------------------------------------------------------------------


def test_derived_artefact_inherits_the_sensitivity_of_its_ancestor() -> None:
    # Test 4. An agent reads a confidential document, summarises it, and writes
    # the summary. The summary carries no label of its own; its lineage reaches
    # the confidential source, so the projection says confidential.
    source = _entry("docs/board-minutes.md", "sha256:" + "1" * 64, [], sensitivity="confidential")
    summary = _entry("docs/summary.md", "sha256:" + "2" * 64, [entry_hash(source)], ts_ns=2)

    verdict = sensitivity_for_artefact("docs/summary.md", [source, summary])

    assert verdict.sensitivity is SensitivityClass.CONFIDENTIAL
    assert verdict.resolved is True
    assert verdict.raised_by == entry_hash(source)


def test_sensitivity_propagates_through_multiple_hops() -> None:
    src = _entry("docs/board-minutes.md", "sha256:" + "1" * 64, [], sensitivity="confidential")
    notes = _entry("docs/notes.md", "sha256:" + "2" * 64, [entry_hash(src)], ts_ns=2)
    slide = _entry("docs/slide.md", "sha256:" + "3" * 64, [entry_hash(notes)], ts_ns=3)

    verdict = effective_sensitivity(entry_hash(slide), [src, notes, slide])

    assert verdict.sensitivity is SensitivityClass.CONFIDENTIAL
    assert verdict.path == (entry_hash(slide), entry_hash(notes), entry_hash(src))


def test_sensitivity_for_artefact_resolves_the_latest_tip() -> None:
    src = _entry("docs/board-minutes.md", "sha256:" + "1" * 64, [], sensitivity="confidential")
    first = _entry("docs/summary.md", "sha256:" + "2" * 64, [], ts_ns=2, sensitivity="public")
    second = _entry("docs/summary.md", "sha256:" + "3" * 64, [entry_hash(first), entry_hash(src)], ts_ns=3)

    verdict = sensitivity_for_artefact("docs/summary.md", [src, first, second])

    assert verdict.target == entry_hash(second)
    assert verdict.sensitivity is SensitivityClass.CONFIDENTIAL


def test_unknown_artefact_path_fails_closed_high() -> None:
    verdict = sensitivity_for_artefact("docs/nowhere.md", [])
    assert verdict.resolved is False
    assert verdict.sensitivity is SensitivityClass.RESTRICTED


# ---------------------------------------------------------------------------
# 5. The verdict names what raised the level
# ---------------------------------------------------------------------------


def test_verdict_names_the_closure_member_that_raised_the_level() -> None:
    # Test 5. "This is confidential" invites an argument; "this is confidential
    # because it derives, through these hops, from that entry" ends it.
    benign = _entry("docs/public.md", "sha256:" + "1" * 64, [], sensitivity="public")
    classified = _entry("docs/board-minutes.md", "sha256:" + "2" * 64, [], sensitivity="restricted")
    mid = _entry("docs/notes.md", "sha256:" + "3" * 64, [entry_hash(classified)], ts_ns=2)
    summary = _entry("docs/summary.md", "sha256:" + "4" * 64, [entry_hash(benign), entry_hash(mid)], ts_ns=3)

    verdict = effective_sensitivity(entry_hash(summary), [benign, classified, mid, summary])

    assert verdict.sensitivity is SensitivityClass.RESTRICTED
    assert verdict.raised_by == entry_hash(classified)
    # The path is the walk through the graph from the target down to the
    # member that raised the level, so an operator can follow the hops.
    assert verdict.path == (entry_hash(summary), entry_hash(mid), entry_hash(classified))
    # Every signed label the verdict projected from is reported.
    assert verdict.sensitivity_records == tuple(
        sorted([(entry_hash(benign), "public"), (entry_hash(classified), "restricted")])
    )


def test_verdict_blames_nothing_when_the_level_came_from_the_fail_closed_default() -> None:
    lone = _entry("docs/lone.md", "sha256:" + "9" * 64, [])
    verdict = effective_sensitivity(entry_hash(lone), [lone])
    assert verdict.raised_by is None
    assert verdict.path == ()
    assert verdict.sensitivity_records == ()


def test_verdict_blames_the_nearest_raising_member_on_a_tie() -> None:
    # Two members carry the same top class at different depths. The verdict
    # names the nearest one, so the shortest explanation is the reported one.
    far = _entry("docs/far.md", "sha256:" + "1" * 64, [], sensitivity="confidential")
    hop = _entry("docs/hop.md", "sha256:" + "2" * 64, [entry_hash(far)], ts_ns=2)
    near = _entry("docs/near.md", "sha256:" + "3" * 64, [], sensitivity="confidential", ts_ns=3)
    target = _entry("docs/t.md", "sha256:" + "4" * 64, [entry_hash(hop), entry_hash(near)], ts_ns=4)

    verdict = effective_sensitivity(entry_hash(target), [far, hop, near, target])

    assert verdict.raised_by == entry_hash(near)
    assert verdict.path == (entry_hash(target), entry_hash(near))


# ---------------------------------------------------------------------------
# 6. Deterministic and recomputable offline
# ---------------------------------------------------------------------------


def test_projection_is_deterministic_and_recomputable_offline(tmp_path: Path) -> None:
    # Test 6. The verdict is a pure function of the log: independent of input
    # order, and reproducible by a verifier holding only ``log.jsonl``.
    src = _entry("docs/board-minutes.md", "sha256:" + "1" * 64, [], sensitivity="confidential")
    mid = _entry("docs/notes.md", "sha256:" + "2" * 64, [entry_hash(src)], ts_ns=2)
    summary = _entry("docs/summary.md", "sha256:" + "3" * 64, [entry_hash(mid)], ts_ns=3)
    entries = [src, mid, summary]
    target = entry_hash(summary)

    in_order = effective_sensitivity(target, entries)
    reversed_order = effective_sensitivity(target, list(reversed(entries)))
    assert in_order == reversed_order
    assert in_order.closure == reversed_order.closure

    # Offline: write the canonical log and recompute from the bytes alone.
    log_path = tmp_path / "log.jsonl"
    log_path.write_bytes(b"".join(canonicalise(e) + b"\n" for e in entries))
    offline = effective_sensitivity(target, load_entries_from_log(log_path))
    assert offline == in_order


# ---------------------------------------------------------------------------
# 7. The classification cannot be dropped by breaking the edge
# ---------------------------------------------------------------------------

_OP_SECRET = b"op-secret-xyz"


class _Agent:
    def __init__(self, agent_id: str, kid: str) -> None:
        self.agent_id = agent_id
        self.kid = kid
        self.priv, self.pub = generate_keypair()
        self.card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=self.pub)


def _signed_entry(
    agent: _Agent,
    path: str,
    content_hash: str,
    parents: list[str],
    *,
    sensitivity: str | None = None,
    ts_ns: int = 1,
) -> LineageEntry:
    fields: dict[str, object] = dict(
        v=1,
        artefact_path=path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parents,
        agent_id=agent.agent_id,
        agent_card_kid=agent.kid,
        tool_call_id="tc",
        span_id="s",
        ts_ns=ts_ns,
        sensitivity=sensitivity,
    )
    unsigned = LineageEntry(operator_hmac="", **fields)  # type: ignore[arg-type]
    return LineageEntry(operator_hmac=compute_operator_hmac(unsigned, _OP_SECRET), **fields)  # type: ignore[arg-type]


def _write_card(cards_dir: Path, agent: _Agent) -> None:
    d = cards_dir / agent.agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": "a2a/1.0",
                "agent_id": agent.agent_id,
                "kid": agent.kid,
                "public_key_pem": agent.pub,
            }
        )
    )


def _write_log_and_sigs(log_path: Path, entries: list[LineageEntry], agent: _Agent) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sig_root = log_path.parent / "signatures"
    with log_path.open("wb") as f:
        for e in entries:
            f.write(canonicalise(e) + b"\n")
    for e in entries:
        jws = sign_detached(canonicalise(e), agent.priv, kid=agent.kid)
        eh = entry_hash(e)
        path_hash = hashlib.sha256(e.artefact_path.encode()).hexdigest()
        dest = sig_root / path_hash[:2] / path_hash
        dest.mkdir(parents=True, exist_ok=True)
        (dest / (eh.replace("sha256:", "") + ".jws")).write_text(jws)


def test_reparenting_to_drop_a_classified_ancestor_fails_signature_verification(tmp_path: Path) -> None:
    # Test 7. Stripping the classification means breaking the parent edge, and
    # that fails the same signature / HMAC / anchoring checks the lineage gate
    # already enforces. Nor does breaking it buy an unclassified artefact: the
    # orphaned summary falls to the fail-closed-high default instead.
    agent = _Agent("agent:gw", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, agent)
    log = tmp_path / "lineage" / "log.jsonl"

    source = _signed_entry(
        agent, "docs/board-minutes.md", "sha256:" + "1" * 64, [], sensitivity="confidential", ts_ns=1
    )
    summary = _signed_entry(agent, "docs/summary.md", "sha256:" + "2" * 64, [entry_hash(source)], ts_ns=2)
    _write_log_and_sigs(log, [source, summary], agent)

    assert check(log_path=log, agent_cards_dir=cards, operator_secret=_OP_SECRET).ok is True
    clean = sensitivity_for_artefact("docs/summary.md", load_entries_from_log(log))
    assert clean.sensitivity is SensitivityClass.CONFIDENTIAL

    # Cut the edge back to the classified source.
    lines = log.read_text().splitlines()
    obj = json.loads(lines[1])
    obj["parent_hashes"] = []
    lines[1] = json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    log.write_text("\n".join(lines) + "\n")

    assert check(log_path=log, agent_cards_dir=cards, operator_secret=_OP_SECRET).ok is False
    orphaned = sensitivity_for_artefact("docs/summary.md", load_entries_from_log(log))
    assert orphaned.sensitivity is SensitivityClass.RESTRICTED
