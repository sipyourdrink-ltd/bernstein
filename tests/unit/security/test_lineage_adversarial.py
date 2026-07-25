"""Adversarial / red-team tests for lineage v1 (ADR-009 §12.6).

Five attack classes:

  1. Replay attack - old entry replayed into a new run.
  2. Substitution attack - swap two entries' signatures.
  3. Privilege escalation - worker writes a merge entry.
  4. Forge attack - synthetic JWS with fake kid.
  5. Path traversal in artefact_path.

Each test sets up the minimum on-disk lineage state required to exercise
the attack and asserts the gate (or the constructor) refuses to accept it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.gate import check
from bernstein.core.lineage.identity import (
    AgentCard,
    generate_keypair,
    sign_detached,
    verify_detached,
)


def _h(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


class _Agent:
    def __init__(self, agent_id: str, kid: str) -> None:
        self.agent_id = agent_id
        self.kid = kid
        self.priv, self.pub = generate_keypair()
        self.card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=self.pub)


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


def _entry(
    agent: _Agent,
    artefact_path: str,
    content_hash: str,
    parent_hashes: list[str],
    ts_ns: int,
    *,
    span_id: str = "span",
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent.agent_id,
        agent_card_kid=agent.kid,
        tool_call_id=f"tc-{ts_ns}",
        span_id=span_id,
        ts_ns=ts_ns,
        operator_hmac="deadbeef" * 8,
    )


def _sidecar_path(log_path: Path, entry: LineageEntry) -> Path:
    """Return the signature sidecar location the store writes and the gate reads.

    The layout is content-addressed: the directory is derived from
    ``sha256(artefact_path)``, so the artefact path is never joined onto a
    filesystem path. ``test_attack_5`` pins that this is the only location
    honoured.
    """
    digest = hashlib.sha256(entry.artefact_path.encode()).hexdigest()
    eh = entry_hash(entry)
    return log_path.parent / "signatures" / digest[:2] / digest / (eh.replace("sha256:", "") + ".jws")


def _append(log_path: Path, entry: LineageEntry, agent: _Agent) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Append the canonical JCS bytes the real ``LineageStore.append`` writes.
    # The gate binds verification to the on-disk bytes (issue #1848), so the
    # fixture must match the canonical form, not ``json.dumps`` defaults.
    canonical = canonicalise(entry)
    with log_path.open("ab") as f:
        f.write(canonical + b"\n")
    jws = sign_detached(canonical, agent.priv, kid=agent.kid)
    sidecar = _sidecar_path(log_path, entry)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(jws)


# ── 1. Replay attack ────────────────────────────────────────────────────────


def test_attack_1_replay_old_entry_with_stale_span_id(tmp_path: Path) -> None:
    """An attacker copies an entry from a previous run and appends it into a
    fresh log. The replayed entry is byte-for-byte identical to the original,
    so its signature verifies - but the gate must surface the duplicate as a
    fork (two children of the same parent with the same hash) AND any
    parent_hash referenced in the replay must still resolve.

    Concretely: replaying without bringing the parent forward leaves the
    chain dangling → gate FAIL.
    """
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    a = _Agent("agent:a", "k1")
    _write_card(cards, a)
    # Attacker has a captured entry from prior run referencing an ancestor
    # that doesn't exist in the new log.
    replay = _entry(a, "x.py", _h("2"), [_h("9")], ts_ns=999, span_id="old-run-span")
    _append(log, replay, a)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("dangling parent_hash" in f for f in result.failures)


def test_attack_1b_duplicate_entry_in_same_log_is_surfaced(tmp_path: Path) -> None:
    """Replaying the same entry into the same log produces two physical lines
    with the same entry_hash. The gate surfaces this as a duplicate-tip
    condition - silent duplication is NOT allowed (it would let an attacker
    smuggle a 'second write' that looks identical).
    """
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    a = _Agent("agent:a", "k1")
    _write_card(cards, a)
    g = _entry(a, "x.py", _h("1"), [], ts_ns=1)
    _append(log, g, a)
    _append(log, g, a)
    result = check(log_path=log, agent_cards_dir=cards)
    # Gate flags two open tips with the same hash - surfaced, not silenced.
    assert result.ok is False
    assert any("tip" in f.lower() or "duplicate" in f.lower() for f in result.failures)


# ── 2. Substitution attack ──────────────────────────────────────────────────


def test_attack_2_substitution_of_signature_fails(tmp_path: Path) -> None:
    """Swap the JWS of entry A with the JWS of entry B; the gate must
    detect the mismatch because each JWS is bound to its entry's canonical
    bytes."""
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    a = _Agent("agent:a", "k1")
    _write_card(cards, a)
    e1 = _entry(a, "x.py", _h("1"), [], ts_ns=1)
    e2 = _entry(a, "y.py", _h("2"), [], ts_ns=2)
    _append(log, e1, a)
    _append(log, e2, a)
    # Swap the JWS files.
    h1 = hashlib.sha256(b"x.py").hexdigest()
    h2 = hashlib.sha256(b"y.py").hexdigest()
    eh1 = entry_hash(e1).replace("sha256:", "")
    eh2 = entry_hash(e2).replace("sha256:", "")
    sig1 = log.parent / "signatures" / h1[:2] / h1 / (eh1 + ".jws")
    sig2 = log.parent / "signatures" / h2[:2] / h2 / (eh2 + ".jws")
    s1 = sig1.read_text()
    s2 = sig2.read_text()
    sig1.write_text(s2)
    sig2.write_text(s1)
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("signature" in f.lower() for f in result.failures)


# ── 3. Privilege escalation ─────────────────────────────────────────────────


def test_attack_3_worker_writes_merge_entry_rejected(tmp_path: Path) -> None:
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    worker = _Agent("agent:worker", "k1")
    _write_card(cards, worker)
    g = _entry(worker, "x.py", _h("1"), [], ts_ns=1)
    f1 = _entry(worker, "x.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _entry(worker, "x.py", _h("3"), [entry_hash(g)], ts_ns=3)
    rogue = _entry(worker, "x.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    for e in [g, f1, f2, rogue]:
        _append(log, e, worker)
    result = check(
        log_path=log,
        agent_cards_dir=cards,
        steward_allowlist=frozenset({"agent:steward"}),
    )
    assert result.ok is False
    assert any("non-steward" in f or "not in allowlist" in f for f in result.failures)


# ── 4. Forge attack - synthetic JWS with fake kid ──────────────────────────


def test_attack_4_forge_synthetic_jws_with_unknown_kid(tmp_path: Path) -> None:
    """Attacker generates their own keypair and signs an entry that claims
    a kid not present in any Agent Card. verify_detached must refuse to
    match a kid that isn't in the card-driven registry."""
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    legit = _Agent("agent:legit", "k1")
    _write_card(cards, legit)
    # Attacker keypair NOT in cards directory.
    attacker_priv, attacker_pub = generate_keypair()
    forged = _entry(legit, "x.py", _h("1"), [], ts_ns=1)
    # Hijack the log entry to claim a different kid the attacker controls.
    forged_dict = asdict(forged)
    forged_dict["agent_card_kid"] = "attacker-kid"
    forged_entry = LineageEntry(**forged_dict)
    log.parent.mkdir(parents=True, exist_ok=True)
    # Write the canonical bytes so the forged entry clears the byte-canonical
    # check (issue #1848) and reaches the kid/signature path under test.
    canonical = canonicalise(forged_entry)
    with log.open("ab") as f:
        f.write(canonical + b"\n")
    # Attacker signs with their own key + their own kid.
    jws = sign_detached(canonical, attacker_priv, kid="attacker-kid")
    digest = hashlib.sha256(b"x.py").hexdigest()
    sig_dir = log.parent / "signatures" / digest[:2] / digest
    sig_dir.mkdir(parents=True, exist_ok=True)
    eh = entry_hash(forged_entry).replace("sha256:", "")
    (sig_dir / (eh + ".jws")).write_text(jws)
    # Sanity: the forged JWS does NOT verify against the legit card.
    assert verify_detached(canonical, jws, legit.card) is False
    # Even though attacker_pub would technically verify, no card lists it.
    fake_card = AgentCard(agent_id="agent:legit", kid="attacker-kid", public_key_pem=attacker_pub)
    assert verify_detached(canonical, jws, fake_card) is True
    # The gate must NOT consult attacker-provided keys - only those under
    # cards_dir. Therefore the gate fails.
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False


# ── 5. Path traversal ──────────────────────────────────────────────────────


def test_attack_5_path_traversal_does_not_escape_lineage_dir(tmp_path: Path) -> None:
    """An entry declaring a traversing artefact_path must not steer the gate's
    signature lookup outside ``log_path.parent``.

    The recorder is the layer that should refuse the path; the gate's contract
    here is "no escape" - signature lookup is always sharded by
    sha256(artefact_path) under ``log_dir/signatures/``, so the attacker's
    string is never joined onto a filesystem path.

    Asserted in both directions against the gate's own behaviour:

      1. the sidecar at the content-addressed location IS honoured, and
      2. a byte-identical sidecar planted where the traversal points, outside
         ``log_dir``, is NOT - the gate reports the signature missing.

    A gate that joined ``artefact_path`` onto ``log_dir/signatures`` would pass
    (1) and fail (2). The traversal used escapes ``log_dir`` while staying
    inside ``tmp_path``, so the test never writes outside its own sandbox.
    """
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    a = _Agent("agent:a", "k1")
    _write_card(cards, a)
    traversing = "../../escape/x.py"
    bad = _entry(a, traversing, _h("1"), [], ts_ns=1)
    _append(log, bad, a)

    sidecar = _sidecar_path(log, bad)
    assert sidecar.is_file()
    assert sidecar.resolve().is_relative_to(log.parent.resolve())

    # 1. The content-addressed sidecar is the one the gate reads.
    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is True, result.failures

    # 2. Move the same signature bytes to where the traversal points. The
    #    naive join escapes log_dir, so a gate that followed the attacker's
    #    string would still find a valid signature and stay green.
    naive_dir = (log.parent / "signatures" / traversing).resolve()
    assert not naive_dir.is_relative_to(log.parent.resolve())
    assert naive_dir.is_relative_to(tmp_path.resolve())
    naive_dir.mkdir(parents=True, exist_ok=True)
    (naive_dir / sidecar.name).write_text(sidecar.read_text())
    sidecar.unlink()

    result = check(log_path=log, agent_cards_dir=cards)
    assert result.ok is False
    assert any("missing signature sidecar" in f for f in result.failures), result.failures


# ── Bonus: forged HMAC ─────────────────────────────────────────────────────


def test_attack_6_forged_operator_hmac_detected_when_secret_known(tmp_path: Path) -> None:
    """The operator HMAC is the second line of defence below the Ed25519
    signature. With the correct operator secret, the gate detects forged
    HMACs even if the signature still verifies (e.g. an insider with
    access to the agent's private key but not the operator secret).
    """
    log = tmp_path / "lineage" / "log.jsonl"
    cards = tmp_path / "agents"
    a = _Agent("agent:a", "k1")
    _write_card(cards, a)
    # Build an entry whose operator_hmac is a *valid hex string* (passes
    # entry schema) but does NOT match the operator secret.
    e = _entry(a, "x.py", _h("1"), [], ts_ns=1)
    _append(log, e, a)
    # With the right operator secret, the entry's mock HMAC should NOT match.
    result = check(
        log_path=log,
        agent_cards_dir=cards,
        operator_secret=b"the-real-operator-secret",
    )
    assert result.ok is False
    assert any("hmac" in f.lower() for f in result.failures)
