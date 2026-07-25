"""Tests for the lineage CI gate (ADR-009 §6.2)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import TypedDict

from bernstein.core.lineage.entry import LineageEntry, canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.gate import GateResult, check
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached

# ── Test fixtures and helpers ───────────────────────────────────────────────


class _AgentCardPayload(TypedDict):
    """On-disk ``card.json`` shape. Typed so the payload is explicit under
    strict type-checking instead of an untyped inline dict."""

    protocolVersion: str
    agent_id: str
    kid: str
    public_key_pem: str


def _card_payload(agent_id: str, kid: str, public_key_pem: str) -> _AgentCardPayload:
    """Build a typed ``card.json`` payload for the given identity."""
    return {
        "protocolVersion": "a2a/1.0",
        "agent_id": agent_id,
        "kid": kid,
        "public_key_pem": public_key_pem,
    }


class _TestAgent:
    def __init__(self, agent_id: str, kid: str) -> None:
        self.agent_id = agent_id
        self.kid = kid
        self.priv, self.pub = generate_keypair()
        self.card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=self.pub)


def _entry_for(
    agent: _TestAgent,
    artefact_path: str,
    content_hash: str,
    parent_hashes: list[str],
    *,
    ts_ns: int = 1_715_600_000_000_000_000,
    operator_secret: bytes = b"op-secret",
) -> LineageEntry:
    unsigned = LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent.agent_id,
        agent_card_kid=agent.kid,
        tool_call_id="tc-x",
        span_id="span-x",
        ts_ns=ts_ns,
        operator_hmac="",
    )
    op_hmac = compute_operator_hmac(unsigned, operator_secret)
    return LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent.agent_id,
        agent_card_kid=agent.kid,
        tool_call_id="tc-x",
        span_id="span-x",
        ts_ns=ts_ns,
        operator_hmac=op_hmac,
    )


def _h(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _write_card(cards_dir: Path, agent: _TestAgent) -> None:
    d = cards_dir / agent.agent_id
    d.mkdir(parents=True, exist_ok=True)
    payload = _card_payload(agent.agent_id, agent.kid, agent.pub)
    (d / "card.json").write_text(json.dumps(payload))


def _shard(s: str) -> str:
    # "sha256:abcd..." → first 2 chars after the prefix
    digest = s.split(":", 1)[1]
    return digest[:2]


def _write_log_and_sigs(
    log_path: Path,
    entries: list[tuple[LineageEntry, str | None]],  # (entry, agent_priv_or_none)
    agents_by_id: dict[str, _TestAgent],
) -> None:
    """Write the log.jsonl + per-entry detached JWS sidecars.

    The log is written with the exact JCS-canonical bytes ``LineageStore.append``
    emits (minimal separators + trailing ``\\n``). The gate binds verification
    to the on-disk bytes (issue #1848), so a faithful writer must match the
    canonical form rather than ``json.dumps`` defaults (which insert ``", "`` /
    ``": "`` spacing and would be rejected as non-canonical).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sig_root = log_path.parent / "signatures"
    sig_root.mkdir(exist_ok=True)
    with log_path.open("wb") as f:
        for entry, _ in entries:
            f.write(canonicalise(entry) + b"\n")
    for entry, _ in entries:
        agent = agents_by_id[entry.agent_id]
        canonical = canonicalise(entry)
        jws = sign_detached(canonical, agent.priv, kid=agent.kid)
        eh = entry_hash(entry)
        # Path-shard + entry-hash filename
        path_hash = hashlib.sha256(entry.artefact_path.encode()).hexdigest()
        dest_dir = sig_root / path_hash[:2] / path_hash
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / (eh.replace("sha256:", "") + ".jws")).write_text(jws)


# ── Happy path ──────────────────────────────────────────────────────────────


def test_gate_passes_on_clean_chain(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    c1 = _entry_for(a, "src/x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    _write_log_and_sigs(log, [(g, None), (c1, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert isinstance(result, GateResult)
    assert result.ok is True
    assert result.failures == []


def test_gate_passes_when_log_missing(tmp_path: Path) -> None:
    cards = tmp_path / "agents"
    cards.mkdir()
    # No log written.
    result = check(log_path=tmp_path / "lineage" / "log.jsonl", agent_cards_dir=cards)
    assert result.ok is True


# ── Unresolved fork ─────────────────────────────────────────────────────────


def test_gate_fails_on_unresolved_fork(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(a, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    _write_log_and_sigs(log, [(g, None), (f1, None), (f2, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("unresolved" in f.lower() or "fork" in f.lower() for f in result.failures)


def test_gate_passes_after_merge_resolves_fork(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    s = _TestAgent("agent:steward", "ks")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    _write_card(cards, s)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(a, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    m = _entry_for(s, "x.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    _write_log_and_sigs(
        log,
        [(g, None), (f1, None), (f2, None), (m, None)],
        {a.agent_id: a, s.agent_id: s},
    )
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures


# ── Signature tampering ─────────────────────────────────────────────────────


def test_gate_fails_on_missing_signature(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    _write_log_and_sigs(log, [(g, None)], {a.agent_id: a})
    # Delete the JWS sidecar.
    sig_root = log.parent / "signatures"
    for jws_file in sig_root.rglob("*.jws"):
        jws_file.unlink()
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("signature" in f.lower() or "missing" in f.lower() for f in result.failures)


def test_gate_fails_on_tampered_entry(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    _write_log_and_sigs(log, [(g, None)], {a.agent_id: a})
    # Tamper with the log: flip a byte in content_hash.
    raw = log.read_text()
    raw_tampered = raw.replace(_h("1"), _h("9"), 1)
    assert raw_tampered != raw
    log.write_text(raw_tampered)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("signature" in f.lower() for f in result.failures)


def test_gate_fails_on_tampered_hmac(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    # Build an entry, then write directly with mangled HMAC.
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    # Replace with garbage hmac. The entry stays internally canonical (only the
    # HMAC *value* is wrong), so it passes the byte-canonical check and reaches
    # the HMAC verification, which is the path under test.
    bad_dict = asdict(g)
    bad_dict["operator_hmac"] = "deadbeef" * 8
    log.parent.mkdir(parents=True, exist_ok=True)
    bad_entry = LineageEntry(**bad_dict)
    canonical = canonicalise(bad_entry)
    log.write_bytes(canonical + b"\n")
    # Write a valid JWS for the bad entry.
    jws = sign_detached(canonical, a.priv, kid=a.kid)
    eh = entry_hash(bad_entry)
    path_hash = hashlib.sha256(bad_entry.artefact_path.encode()).hexdigest()
    dest_dir = log.parent / "signatures" / path_hash[:2] / path_hash
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / (eh.replace("sha256:", "") + ".jws")).write_text(jws)
    # Provide the operator secret that won't match.
    result = check(log_path=log, agent_cards_dir=cards, operator_secret=b"op-secret")
    assert result.ok is False
    assert any("hmac" in f.lower() for f in result.failures)


# ── Parent chain integrity ──────────────────────────────────────────────────


def test_gate_fails_on_dangling_parent(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    orphan = _entry_for(a, "x.py", _h("2"), [_h("nonexistent")], ts_ns=2)
    _write_log_and_sigs(log, [(orphan, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("parent" in f.lower() or "chain" in f.lower() for f in result.failures)


# ── Missing Agent Card ──────────────────────────────────────────────────────


def test_gate_fails_when_agent_card_unknown(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    cards.mkdir(parents=True, exist_ok=True)
    # Do NOT write card for `a`.
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    _write_log_and_sigs(log, [(g, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("card" in f.lower() or "unknown agent" in f.lower() for f in result.failures)


# ── Steward allow-list (privilege escalation) ───────────────────────────────


def test_gate_rejects_worker_writing_merge_entry(tmp_path: Path) -> None:
    worker = _TestAgent("agent:worker", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, worker)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(worker, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(worker, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(worker, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    # Worker tries to write a merge entry - must be rejected if allow-list set.
    m = _entry_for(worker, "x.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    _write_log_and_sigs(log, [(g, None), (f1, None), (f2, None), (m, None)], {worker.agent_id: worker})
    result = check(
        log_path=log,
        agent_cards_dir=cards,
        steward_allowlist=frozenset({"agent:steward"}),
    )
    assert result.ok is False
    assert any("merge" in f.lower() or "steward" in f.lower() for f in result.failures)


def test_gate_allows_worker_merge_when_no_allowlist_configured(tmp_path: Path) -> None:
    """Default: no allow-list => no privilege check. Steward role is policy-only."""
    worker = _TestAgent("agent:worker", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, worker)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(worker, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(worker, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(worker, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    m = _entry_for(worker, "x.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    _write_log_and_sigs(log, [(g, None), (f1, None), (f2, None), (m, None)], {worker.agent_id: worker})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True


# ── GateResult shape ────────────────────────────────────────────────────────


def test_gate_result_is_tuple_like(tmp_path: Path) -> None:
    cards = tmp_path / "agents"
    cards.mkdir()
    result = check(log_path=tmp_path / "noop.jsonl", agent_cards_dir=cards)
    assert hasattr(result, "ok")
    assert hasattr(result, "failures")
    assert isinstance(result.failures, list)


def test_gate_handles_corrupt_log_line(tmp_path: Path) -> None:
    cards = tmp_path / "agents"
    cards.mkdir()
    log = tmp_path / "lineage" / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("{not-json\n")
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("parse" in f.lower() or "corrupt" in f.lower() for f in result.failures)


def test_gate_handles_card_directory_missing(tmp_path: Path) -> None:
    log = tmp_path / "lineage" / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    # No agents dir.
    result = check(log_path=log, agent_cards_dir=tmp_path / "no-such-dir")
    assert result.ok is True  # empty log → OK regardless of cards dir


# ── Property-style: HMAC matches when operator_secret given ────────────────


def test_gate_verifies_hmac_when_secret_provided(tmp_path: Path) -> None:
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1, operator_secret=b"op-secret")
    _write_log_and_sigs(log, [(g, None)], {a.agent_id: a})
    # Wrong operator secret → HMAC mismatch.
    bad = check(log_path=log, agent_cards_dir=cards, operator_secret=b"WRONG")
    assert bad.ok is False
    # Right operator secret → OK.
    good = check(log_path=log, agent_cards_dir=cards, operator_secret=b"op-secret")
    assert good.ok is True, good.failures


def test_gate_accepts_recorder_emitted_entries_under_operator_secret(tmp_path: Path) -> None:
    """Regression: recorder and gate must agree on the operator-HMAC body.

    Previously the gate computed HMAC over a 3-field body (``{p, h, ts}``)
    while the recorder used the full JCS-canonical entry body with the
    ``operator_hmac`` field blanked. Production gate runs against real
    recorder output failed 100% of entries. Pin the agreement end-to-end.
    """
    from bernstein.core.lineage.identity import generate_keypair
    from bernstein.core.lineage.signed_write import SignedLineageLog
    from bernstein.core.lineage.store import LineageStore

    priv, pub = generate_keypair()
    card = AgentCard(agent_id="agent:rec-1", kid="kid-rec-1", public_key_pem=pub)
    # Write the card to disk so the gate can resolve the signing key.
    cards_dir = tmp_path / "agents"
    (cards_dir / card.agent_id).mkdir(parents=True)
    (cards_dir / card.agent_id / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": "a2a/1.0",
                "agent_id": card.agent_id,
                "kid": card.kid,
                "public_key_pem": pub,
            }
        )
    )

    operator_secret = b"recorder-gate-shared-secret"
    store = LineageStore(tmp_path / "lineage")
    recorder = SignedLineageLog(store=store, operator_hmac_key=operator_secret)
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"hello",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )

    log_path = tmp_path / "lineage" / "log.jsonl"
    # Right secret → gate accepts the recorder's entry.
    good = check(log_path=log_path, agent_cards_dir=cards_dir, operator_secret=operator_secret)
    assert good.ok is True, good.failures
    # Wrong secret → gate flags HMAC mismatch.
    bad = check(log_path=log_path, agent_cards_dir=cards_dir, operator_secret=b"different-secret")
    assert bad.ok is False
    assert any("HMAC mismatch" in f for f in bad.failures), bad.failures


# ── Mutation-killing tests (close survivor gaps) ────────────────────────────


def test_gate_rejects_malformed_agent_card_with_non_string_field(tmp_path: Path) -> None:
    """`_load_cards` must skip cards where any of agent_id/kid/public_key_pem
    is not a string. Kills the `and -> or` mutation in the isinstance chain."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    card_dir = cards / "agent:bad"
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": "a2a/1.0",
                "agent_id": "agent:bad",
                "kid": None,  # ← invalid; should cause card to be skipped
                "public_key_pem": a.pub,
            }
        )
    )
    # Also a valid card for agent:a so we can issue a real entry.
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    _write_log_and_sigs(log, [(g, None)], {a.agent_id: a})
    # The bad card was skipped, but agent:a is fine → gate passes.
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True
    # Now write an entry from agent:bad with no valid card → gate fails.
    bad = _TestAgent("agent:bad", "k1")
    g2 = _entry_for(bad, "y.py", _h("2"), [], ts_ns=2)
    _write_log_and_sigs(log, [(g, None), (g2, None)], {a.agent_id: a, bad.agent_id: bad})
    result2 = check(log_path=log, agent_cards_dir=cards)
    assert result2.ok is False
    assert any("agent:bad" in f and ("card" in f.lower() or "unknown" in f.lower()) for f in result2.failures)


def test_gate_steward_allowlist_does_not_check_non_merge_entries(tmp_path: Path) -> None:
    """The steward allow-list only applies when parent_hashes length >= 2.
    A genesis or single-parent entry from a non-allowed agent must NOT
    trigger the privilege check. Kills the `>= 2 -> >= 1` mutation."""
    a = _TestAgent("agent:worker", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    c1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    _write_log_and_sigs(log, [(g, None), (c1, None)], {a.agent_id: a})
    # Allowlist excludes "agent:worker", but no merge entries exist → pass.
    result = check(
        log_path=log,
        agent_cards_dir=cards,
        steward_allowlist=frozenset({"agent:steward"}),
    )
    assert result.ok is True, result.failures


def test_gate_failure_count_appears_in_failure_message(tmp_path: Path) -> None:
    """The open-tip failure message must contain the actual count, not zero.
    Kills the `len( -> 0 * len(` mutation."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(a, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    f3 = _entry_for(a, "x.py", _h("4"), [entry_hash(g)], ts_ns=4)
    _write_log_and_sigs(
        log,
        [(g, None), (f1, None), (f2, None), (f3, None)],
        {a.agent_id: a},
    )
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    # Match exact count "3 unresolved" - kills the zero-count mutation.
    tip_msgs = [f for f in result.failures if "unresolved" in f and "tips" in f]
    assert tip_msgs, result.failures
    assert "3" in tip_msgs[0]


def test_gate_fork_resolved_flag_initial_false_not_true(tmp_path: Path) -> None:
    """The `resolved = False` initialisation must be False, not True, so that
    forks without a covering merge entry are reported. Kills the
    `False -> True` flip on the resolved sentinel.

    We construct a scenario where the open-tip count is OK (1) but a
    historical fork is still unresolved - that forces the gate to rely on
    the resolved=False initial value.

    Concretely: after a merge, write another genesis-style child of the
    pre-merge state. The merge is the open tip; the fork is technically
    closed in compute_tips terms, but detect_forks still surfaces it.
    Easier test: use the existing unresolved-fork shape and check the
    fork-level message appears alongside the tip-level one.
    """
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(a, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    _write_log_and_sigs(log, [(g, None), (f1, None), (f2, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    # The gate must produce a fork-level message too - both classes of
    # message must appear if the initial `resolved = False` is intact.
    fork_msgs = [f for f in result.failures if "unresolved fork" in f]
    assert fork_msgs, f"no fork-level message: {result.failures}"


def test_gate_dangling_parent_hash_count_matches_input(tmp_path: Path) -> None:
    """Multiple dangling parents must each produce a distinct failure message."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    o1 = _entry_for(a, "x.py", _h("2"), [_h("ghost-1")], ts_ns=2)
    o2 = _entry_for(a, "y.py", _h("3"), [_h("ghost-2"), _h("ghost-3")], ts_ns=3)
    _write_log_and_sigs(log, [(o1, None), (o2, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    dangling = [f for f in result.failures if "dangling parent_hash" in f]
    # o1 has 1 ghost, o2 has 2 ghosts → 3 dangling-parent messages.
    assert len(dangling) == 3


def test_compute_tips_distinguishes_open_vs_merged(tmp_path: Path) -> None:
    """Ensure compute_tips classifies merge parents as 'merged', not 'open'.
    Kills mutations that confuse the two sets in tips.py."""
    from bernstein.core.lineage.tips import compute_tips

    a = _TestAgent("agent:a", "k1")
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(a, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    m = _entry_for(a, "x.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    tips = compute_tips([g, f1, f2, m])
    assert entry_hash(f1) not in tips["x.py"]["open"]
    assert entry_hash(f2) not in tips["x.py"]["open"]
    assert entry_hash(m) in tips["x.py"]["open"]
    assert entry_hash(f1) in tips["x.py"]["merged"]
    assert entry_hash(f2) in tips["x.py"]["merged"]


def test_detect_forks_requires_at_least_two_children(tmp_path: Path) -> None:
    """A parent with one child must NOT be reported as a fork.
    Kills the `< 2 -> < 1` mutation in detect_forks."""
    from bernstein.core.lineage.tips import detect_forks

    a = _TestAgent("agent:a", "k1")
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    only_child = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    assert detect_forks([g, only_child]) == []


# ── Kid binding (issue #1837) ───────────────────────────────────────────────
#
# The gate must bind the entry's *signed* ``agent_card_kid`` to the card
# actually used to verify it. Selecting the card by ``agent_id`` alone (a) lets
# a kid-substitution slip through and (b) breaks every historical entry the
# moment an agent rotates its key. These tests pin the binding.


def _write_card_for_kid(cards_dir: Path, agent: _TestAgent) -> None:
    """Write a card under the per-kid layout ``<agent-id>/<kid>/card.json``.

    This is the layout that disambiguates multiple historical keys for one
    agent (post key-rotation). The legacy ``<agent-id>/card.json`` layout is
    written by :func:`_write_card`.
    """
    d = cards_dir / agent.agent_id / agent.kid
    d.mkdir(parents=True, exist_ok=True)
    payload = _card_payload(agent.agent_id, agent.kid, agent.pub)
    (d / "card.json").write_text(json.dumps(payload))


def _sign_entry_into(log_path: Path, entry: LineageEntry, *, priv_pem: str, header_kid: str) -> None:
    """Append ``entry`` to the log and write a detached JWS signed with an
    explicit ``priv_pem`` and JWS-header ``kid`` (which may deliberately
    diverge from the entry's signed ``agent_card_kid``)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sig_root = log_path.parent / "signatures"
    sig_root.mkdir(exist_ok=True)
    # Append the canonical bytes the real writer emits so the gate's
    # byte-canonical check (issue #1848) sees a faithful on-disk form.
    canonical = canonicalise(entry)
    with log_path.open("ab") as f:
        f.write(canonical + b"\n")
    jws = sign_detached(canonical, priv_pem, kid=header_kid)
    eh = entry_hash(entry)
    path_hash = hashlib.sha256(entry.artefact_path.encode()).hexdigest()
    dest_dir = sig_root / path_hash[:2] / path_hash
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / (eh.replace("sha256:", "") + ".jws")).write_text(jws)


def test_gate_passes_with_single_per_kid_card(tmp_path: Path) -> None:
    """An entry signed under kid ``k1`` verifies against a per-kid card for
    ``k1``. This is the rotation-aware happy path for the new layout."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card_for_kid(cards, a)  # <agent-id>/k1/card.json
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, g, priv_pem=a.priv, header_kid=a.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures


def test_gate_fails_when_card_kid_does_not_match_entry_kid(tmp_path: Path) -> None:
    """An entry signed under ``k1`` must FAIL with a clear kid-binding error
    when the only card present for that agent is under a *different* kid
    (``k2``) - even though the card directory has *a* card for the agent.

    This is the substitution gap: previously the gate verified against
    whatever single card sat at ``agent_id`` and ignored ``agent_card_kid``.
    """
    a_k1 = _TestAgent("agent:a", "k1")
    a_k2 = _TestAgent("agent:a", "k2")  # same agent_id, different kid + key
    cards = tmp_path / "agents"
    # Only the k2 card is on disk; the entry is signed under k1.
    _write_card_for_kid(cards, a_k2)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a_k1, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, g, priv_pem=a_k1.priv, header_kid=a_k1.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    # Must be a precise kid-binding failure, NOT a generic signature error.
    assert any("kid" in f.lower() and "k1" in f for f in result.failures), result.failures


def test_gate_passes_for_both_kids_after_rotation(tmp_path: Path) -> None:
    """After an agent rotates its key (old + new card both present, one per
    kid), historical entries under the old kid and new entries under the new
    kid must BOTH pass ``check``."""
    a_old = _TestAgent("agent:a", "k-old")
    a_new = _TestAgent("agent:a", "k-new")
    cards = tmp_path / "agents"
    _write_card_for_kid(cards, a_old)
    _write_card_for_kid(cards, a_new)
    log = tmp_path / "lineage" / "log.jsonl"
    old_entry = _entry_for(a_old, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, old_entry, priv_pem=a_old.priv, header_kid=a_old.kid)
    new_entry = _entry_for(a_new, "src/x.py", _h("2"), [entry_hash(old_entry)], ts_ns=2)
    _sign_entry_into(log, new_entry, priv_pem=a_new.priv, header_kid=a_new.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures


def test_gate_fails_when_header_kid_diverges_from_body_kid(tmp_path: Path) -> None:
    """An entry whose signed body names ``agent_card_kid = "k-old"`` but whose
    JWS header carries ``kid = "k-new"`` must FAIL with a kid-binding error,
    even when a valid card for ``k-new`` exists and the signature would verify
    against it."""
    a_old = _TestAgent("agent:a", "k-old")
    a_new = _TestAgent("agent:a", "k-new")
    cards = tmp_path / "agents"
    # Both cards present so neither side is "missing"; the divergence itself
    # is the failure.
    _write_card_for_kid(cards, a_old)
    _write_card_for_kid(cards, a_new)
    log = tmp_path / "lineage" / "log.jsonl"
    # Body says k-old; sign with k-new's key AND k-new header kid.
    entry = _entry_for(a_old, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, entry, priv_pem=a_new.priv, header_kid=a_new.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("kid" in f.lower() for f in result.failures), result.failures


def test_gate_legacy_single_card_layout_still_passes(tmp_path: Path) -> None:
    """No-rotation regression: a single legacy ``<agent-id>/card.json`` (the
    layout production writes today) must still verify exactly as before."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)  # legacy <agent-id>/card.json
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    c1 = _entry_for(a, "src/x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    _write_log_and_sigs(log, [(g, None), (c1, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures


# ── Card-conflict detection (issue #1837, read-order independence) ──────────
#
# "Last one read wins" makes the verification outcome depend on filesystem
# read order when two distinct cards claim the same ``(agent_id, kid)``. For a
# kid-binding security gate that is a tamper/config-conflict hole, so the
# loader must fail explicitly. Byte-identical duplicates (the legacy + per-kid
# layouts carrying the same key) stay fine.


def _write_raw_card(cards_dir: Path, agent_id: str, kid: str, *, pub: str, sub: str = "") -> None:
    """Write a ``card.json`` with explicit field values under
    ``<agent-id>[/sub]/card.json`` (``sub`` selects the per-kid layout)."""
    d = cards_dir / agent_id
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / "card.json").write_text(json.dumps(_card_payload(agent_id, kid, pub)))


def test_gate_fails_on_conflicting_cards_for_same_agent_id_kid(tmp_path: Path) -> None:
    """Two cards that claim the same ``(agent_id, kid)`` with *different* public
    keys must produce an explicit conflict failure that names the agent_id and
    kid - never a silent read-order-dependent winner."""
    a1 = _TestAgent("agent:a", "k1")
    a2 = _TestAgent("agent:a", "k1")  # same identity, different generated key
    assert a1.pub != a2.pub
    cards = tmp_path / "agents"
    # Legacy layout carries a1's key; per-kid layout carries a2's key. Same
    # (agent_id, kid), divergent public_key_pem -> conflict.
    _write_raw_card(cards, "agent:a", "k1", pub=a1.pub)
    _write_raw_card(cards, "agent:a", "k1", pub=a2.pub, sub="k1")
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a1, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, g, priv_pem=a1.priv, header_kid=a1.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    conflict = [f for f in result.failures if "conflicting agent cards" in f]
    assert conflict, result.failures
    # The message must name the exact identity in conflict.
    assert "agent:a" in conflict[0] and "k1" in conflict[0], conflict[0]


def test_gate_accepts_byte_identical_duplicate_cards(tmp_path: Path) -> None:
    """The legacy ``<agent-id>/card.json`` and the per-kid
    ``<agent-id>/<kid>/card.json`` carrying the *same* key for the same
    ``(agent_id, kid)`` must NOT be flagged as a conflict - identical
    duplicates are the normal cross-layout case."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)  # legacy
    _write_card_for_kid(cards, a)  # per-kid, identical key material
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, g, priv_pem=a.priv, header_kid=a.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures
    assert not any("conflicting" in f for f in result.failures), result.failures


def test_load_cards_skips_card_with_non_string_public_key(tmp_path: Path) -> None:
    """``_load_cards`` must require *all* of agent_id/kid/public_key_pem to be
    strings (the ``and`` chain). A card with a non-string ``public_key_pem``
    but valid id/kid must be skipped, leaving the map empty - this kills the
    ``and -> or`` mutation, which would otherwise admit the malformed card."""
    from bernstein.core.lineage.gate import _load_cards

    cards = tmp_path / "agents"
    d = cards / "agent:a"
    d.mkdir(parents=True, exist_ok=True)
    # agent_id and kid are valid strings; only public_key_pem is wrong type.
    (d / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": "a2a/1.0",
                "agent_id": "agent:a",
                "kid": "k1",
                "public_key_pem": 1234,  # non-string -> whole card must be skipped
            }
        )
    )
    loaded, failures = _load_cards(cards)
    assert loaded == {}, loaded
    assert failures == []


def test_gate_reports_kid_binding_cannot_be_established_message(tmp_path: Path) -> None:
    """When no card exists for the entry's exact ``(agent_id, kid)`` the failure
    text must state the binding *cannot* be established - kills the mutation
    that strips ``not`` from ``cannot`` in that message."""
    a_k1 = _TestAgent("agent:a", "k1")
    a_k2 = _TestAgent("agent:a", "k2")
    cards = tmp_path / "agents"
    _write_card_for_kid(cards, a_k2)  # only k2 on disk; entry signed under k1
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a_k1, "src/x.py", _h("1"), [], ts_ns=1)
    _sign_entry_into(log, g, priv_pem=a_k1.priv, header_kid=a_k1.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("kid binding cannot be established" in f for f in result.failures), result.failures


def test_gate_header_vs_body_kid_message_states_inequality(tmp_path: Path) -> None:
    """The header/body kid-divergence message must spell out the inequality
    (``!=``) between the signed-body kid and the JWS-header kid - kills the
    ``!= -> ==`` mutation in that f-string."""
    a_old = _TestAgent("agent:a", "k-old")
    a_new = _TestAgent("agent:a", "k-new")
    cards = tmp_path / "agents"
    _write_card_for_kid(cards, a_old)
    _write_card_for_kid(cards, a_new)
    log = tmp_path / "lineage" / "log.jsonl"
    entry = _entry_for(a_old, "src/x.py", _h("1"), [], ts_ns=1)
    # Body names k-old; sign with k-new header kid so header != body.
    _sign_entry_into(log, entry, priv_pem=a_new.priv, header_kid=a_new.kid)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    mismatch = [f for f in result.failures if "kid binding mismatch" in f]
    assert mismatch, result.failures
    assert "!=" in mismatch[0], mismatch[0]
    assert "k-old" in mismatch[0] and "k-new" in mismatch[0], mismatch[0]


def test_gate_reports_unreadable_signature_sidecar(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When the signature sidecar exists but cannot be read, the gate must
    surface a 'cannot read signature' failure and keep going - kills the
    mutation stripping ``not`` from ``cannot`` in that branch."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    _write_log_and_sigs(log, [(g, None)], {a.agent_id: a})

    real_read_text = Path.read_text

    def _boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.suffix == ".jws":
            raise OSError("simulated unreadable signature")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _boom)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("cannot read signature" in f for f in result.failures), result.failures


def test_gate_non_steward_merge_message_says_not_in_allowlist(tmp_path: Path) -> None:
    """A merge entry written by a non-allow-listed agent must fail with a
    message that includes 'not in allowlist' - kills the mutation that strips
    ``not`` from that message."""
    worker = _TestAgent("agent:worker", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, worker)
    log = tmp_path / "lineage" / "log.jsonl"
    p1 = _entry_for(worker, "x.py", _h("1"), [], ts_ns=1)
    p2 = _entry_for(worker, "x.py", _h("2"), [], ts_ns=2)
    merge = _entry_for(worker, "x.py", _h("3"), [entry_hash(p1), entry_hash(p2)], ts_ns=3)
    _write_log_and_sigs(log, [(p1, None), (p2, None), (merge, None)], {worker.agent_id: worker})
    result = check(
        log_path=log,
        agent_cards_dir=cards,
        steward_allowlist=frozenset({"agent:steward"}),
    )
    assert result.ok is False
    non_steward = [f for f in result.failures if "non-steward" in f]
    assert non_steward, result.failures
    assert "not in allowlist" in non_steward[0], non_steward[0]


def test_gate_two_open_tips_exact_boundary(tmp_path: Path) -> None:
    """Exactly two unresolved open tips on one artefact must fail (the guard is
    ``> 1``). The reported count must equal 2 - this pins the threshold at 1
    (killing the ``1 -> 2`` mutation, where two tips would no longer trip it)
    and the count operand (killing ``len( -> 0 * len(``)."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry_for(a, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry_for(a, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    _write_log_and_sigs(log, [(g, None), (f1, None), (f2, None)], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    tip_msgs = [f for f in result.failures if "unresolved open tips" in f]
    assert tip_msgs, result.failures
    assert "2 unresolved open tips" in tip_msgs[0], tip_msgs[0]


def test_gate_unrelated_merge_does_not_resolve_a_different_fork(tmp_path: Path) -> None:
    """Fork resolution requires an entry whose parent_hashes cover ALL of the
    fork's children (``len(parents) >= 2 AND children.issubset(parents)``).

    A *different* artefact's legitimate two-parent merge - which satisfies the
    ``>= 2`` half but whose parents do NOT cover the unresolved fork's children
    - must NOT clear that fork. This kills the ``and -> or`` mutation: under
    ``or`` the unrelated merge's parent count alone would wrongly mark the open
    fork resolved, flipping the gate to pass.
    """
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    # Artefact A: an UNRESOLVED fork (g_a forks into c1_a and c2_a, never merged).
    g_a = _entry_for(a, "a.py", _h("1"), [], ts_ns=1)
    c1_a = _entry_for(a, "a.py", _h("2"), [entry_hash(g_a)], ts_ns=2)
    c2_a = _entry_for(a, "a.py", _h("3"), [entry_hash(g_a)], ts_ns=3)
    # Artefact B: a RESOLVED fork - g_b forks, then m_b merges both children.
    # m_b has two parents (satisfies the >= 2 half) but they are {c1_b, c2_b},
    # which do not cover artefact A's children {c1_a, c2_a}.
    g_b = _entry_for(a, "b.py", _h("4"), [], ts_ns=4)
    c1_b = _entry_for(a, "b.py", _h("5"), [entry_hash(g_b)], ts_ns=5)
    c2_b = _entry_for(a, "b.py", _h("6"), [entry_hash(g_b)], ts_ns=6)
    m_b = _entry_for(a, "b.py", _h("7"), [entry_hash(c1_b), entry_hash(c2_b)], ts_ns=7)
    _write_log_and_sigs(
        log,
        [
            (g_a, None),
            (c1_a, None),
            (c2_a, None),
            (g_b, None),
            (c1_b, None),
            (c2_b, None),
            (m_b, None),
        ],
        {a.agent_id: a},
    )
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    fork_msgs = [f for f in result.failures if "unresolved fork" in f]
    # Exactly artefact A's fork is unresolved; B's was correctly merged.
    assert fork_msgs, result.failures
    assert all("a.py" in f for f in fork_msgs), fork_msgs
    assert not any("b.py" in f for f in fork_msgs), fork_msgs


# ── Skill-lockfile admission check (issue #1796) ────────────────────────────
#
# ``check_skill_lockfile`` lives beside ``check`` in gate.py but its existing
# coverage is under tests/unit/core/skills/. The per-module mutation gate runs
# only tests/unit/lineage/ against gate.py, so these lineage-local tests pin
# the lockfile branch logic for that gate too (kills the survivors at the
# is_file / pass-result / membership / final-ok lines).


def _write_catalog_lockfile(path: Path, *, manifest_sha256: str, entry_id: str = "code-review") -> None:
    """Write a minimal single-row ``[[catalog]]`` lockfile (plain TOML) so the
    lineage test stays free of the catalog package's writer."""
    path.write_text(
        "\n".join(
            (
                "[[catalog]]",
                f'id = "{entry_id}"',
                f'name = "{entry_id}"',
                'version = "1.0.0"',
                'manifest_url = "github://acme/code-review@v1"',
                f'manifest_sha256 = "{manifest_sha256}"',
                f'content_digest = "{"2" * 64}"',
                'install_id = "install-1"',
                f'chain_head = "{"3" * 64}"',
                'installed_at = "2026-05-21T00:00:00Z"',
                "",
            )
        )
    )


def test_check_skill_lockfile_passes_when_missing() -> None:
    """A missing lockfile is a no-op pass (``ok=True``, empty failures). Kills
    the ``is_file`` ``not`` strip, the ``True -> False`` flip, and the
    ``[] -> [None]`` mutation on the missing-file return."""
    from bernstein.core.lineage.gate import check_skill_lockfile

    result = check_skill_lockfile(Path("/no/such/skills.lock"), frozenset())
    assert result.ok is True
    assert result.failures == []


def test_check_skill_lockfile_accepts_anchored_row(tmp_path: Path) -> None:
    """A row whose ``manifest_sha256`` is in the known-good set passes with no
    failures. Together with the rejection test this pins ``ok = not failures``
    (both the empty and non-empty branches)."""
    from bernstein.core.lineage.gate import check_skill_lockfile

    sha = "deadbeef" + "0" * 56
    lockfile = tmp_path / "skills.lock"
    _write_catalog_lockfile(lockfile, manifest_sha256=sha)
    result = check_skill_lockfile(lockfile, frozenset({sha}))
    assert result.ok is True, result.failures
    assert result.failures == []


def test_check_skill_lockfile_rejects_unanchored_row(tmp_path: Path) -> None:
    """A row whose ``manifest_sha256`` is NOT in the known-good set must fail.
    Kills the membership ``not`` strip (``not in`` -> ``in``), the final
    ``ok = not failures`` mutation, and the 'is not present' message strip."""
    from bernstein.core.lineage.gate import check_skill_lockfile

    rogue = "rogue" + "0" * 59
    lockfile = tmp_path / "skills.lock"
    _write_catalog_lockfile(lockfile, manifest_sha256=rogue)
    # known-good set holds a *different* sha, so the row is un-anchored.
    result = check_skill_lockfile(lockfile, frozenset({"feed" + "0" * 60}))
    assert result.ok is False
    assert any("code-review" in f for f in result.failures), result.failures
    assert any("is not present" in f for f in result.failures), result.failures


# ── Byte-level tamper-evidence (issue #1848) ────────────────────────────────
#
# The gate must bind verification to the *exact bytes on disk*, the way the
# sibling HMAC audit log (``bernstein.core.security.audit``) already does. A
# verifier that re-canonicalises the parsed entry hashes a normalised form, so
# any non-canonical rewrite that preserves the field values (reordered keys,
# extra whitespace, a flipped record terminator) slips through even though the
# raw bytes - the provenance anchor - were tampered with.


def _write_log_canonical_and_sigs(
    log_path: Path,
    entries: list[LineageEntry],
    agents_by_id: dict[str, _TestAgent],
) -> None:
    """Write the log with the *canonical* JCS bytes ``LineageStore.append``
    emits (minimal separators + trailing ``\\n``), plus per-entry sidecars.

    This mirrors the real on-disk writer so the byte-canonical gate check has
    a faithful happy-path baseline to compare against.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sig_root = log_path.parent / "signatures"
    sig_root.mkdir(exist_ok=True)
    with log_path.open("wb") as f:
        for entry in entries:
            f.write(canonicalise(entry) + b"\n")
    for entry in entries:
        agent = agents_by_id[entry.agent_id]
        canonical = canonicalise(entry)
        jws = sign_detached(canonical, agent.priv, kid=agent.kid)
        eh = entry_hash(entry)
        path_hash = hashlib.sha256(entry.artefact_path.encode()).hexdigest()
        dest_dir = sig_root / path_hash[:2] / path_hash
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / (eh.replace("sha256:", "") + ".jws")).write_text(jws)


def test_gate_passes_on_canonically_written_log(tmp_path: Path) -> None:
    """Baseline: a log written with the exact canonical bytes the store emits
    must verify clean. Pins that the byte-canonical check does not regress the
    real writer's output."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    c1 = _entry_for(a, "src/x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    _write_log_canonical_and_sigs(log, [g, c1], {a.agent_id: a})
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures


def test_gate_rejects_reordered_keys_with_valid_resignature(tmp_path: Path) -> None:
    """A log line rewritten with reordered keys (identical field values, and a
    JWS that re-canonicalises to a valid signature today) must be rejected as
    non-canonical bytes. This is the core tamper-evidence gap: the on-disk
    bytes differ from the canonical form yet verification passes."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    _write_log_canonical_and_sigs(log, [g], {a.agent_id: a})

    # Rewrite the single line with reversed key order, kept on ONE line (no
    # embedded newlines) and with minimal separators so it stays valid JSON.
    # The field values are unchanged, so json.loads -> LineageEntry ->
    # canonicalise yields the exact bytes the JWS was signed over: the
    # signature still verifies. Only the byte-canonical check separates this
    # tampered line from the canonical original.
    obj = json.loads(log.read_bytes())
    noncanonical = (json.dumps(dict(reversed(list(obj.items()))), separators=(",", ":")) + "\n").encode()
    assert noncanonical != canonicalise(g) + b"\n"
    assert b"\n" not in noncanonical[:-1], "tampered line must stay single-line"
    log.write_bytes(noncanonical)

    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False, "non-canonical on-disk bytes must be rejected"
    assert any("canonical" in f.lower() for f in result.failures), (
        f"expected a non-canonical-bytes failure, got {result.failures}"
    )


def test_gate_rejects_extra_whitespace_in_line(tmp_path: Path) -> None:
    """Inserting incidental whitespace inside a record (e.g. a space after the
    opening brace) changes the on-disk bytes without changing any field value;
    the gate must surface it as a non-canonical-bytes failure."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    _write_log_canonical_and_sigs(log, [g], {a.agent_id: a})

    raw = log.read_bytes()
    tampered = raw.replace(b"{", b"{ ", 1)  # space after first brace
    assert tampered != raw
    log.write_bytes(tampered)

    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("canonical" in f.lower() for f in result.failures), result.failures


def test_gate_rejects_flipped_record_terminator(tmp_path: Path) -> None:
    """Flipping an interior line's terminator from ``\\n`` to a lone ``\\r``
    must be surfaced as a failure. Python text-mode iteration silently
    translates a lone ``\\r`` into a record boundary (universal newlines), so
    the two records still frame into two parsable, signature-valid entries -
    a framing change that goes undetected today. Strict ``b"\\n"`` splitting
    plus the byte-canonical check must catch it: the ``\\r`` lands inside a
    record whose bytes no longer equal the canonical form."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    c1 = _entry_for(a, "src/x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    _write_log_canonical_and_sigs(log, [g, c1], {a.agent_id: a})

    raw = log.read_bytes()
    # Flip the FIRST record terminator 0x0A -> 0x0D (lone CR). Text-mode
    # iteration treats it as a line break, so both records survive intact and
    # verify today. Strict b"\n" splitting keeps the \r inside the merged line
    # whose bytes then differ from the canonical form.
    first_nl = raw.index(b"\n")
    tampered = raw[:first_nl] + b"\r" + raw[first_nl + 1 :]
    assert tampered != raw
    log.write_bytes(tampered)

    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False, "flipped record terminator must be rejected"


def test_gate_rejects_missing_trailing_newline(tmp_path: Path) -> None:
    """The writer always terminates the file with ``\\n``; a missing terminator
    is itself tamper-evidence (a truncated or terminator-stripped final record)
    and must fail, mirroring the audit log's ``missing trailing newline``."""
    a = _TestAgent("agent:a", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    g = _entry_for(a, "src/x.py", _h("1"), [], ts_ns=1)
    _write_log_canonical_and_sigs(log, [g], {a.agent_id: a})

    raw = log.read_bytes()
    assert raw.endswith(b"\n")
    log.write_bytes(raw.rstrip(b"\n"))  # strip the terminator

    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False, "missing trailing newline must be rejected"
