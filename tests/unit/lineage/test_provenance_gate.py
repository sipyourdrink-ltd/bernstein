"""Tamper-evidence for provenance records under the existing lineage gate.

A trust class is a signed field of a lineage entry, so mutating a provenance
record or reparenting a lineage edge breaks the same signature/HMAC/anchoring
checks the gate already enforces. ``verify_taint`` refuses to emit a verdict
from a log that does not pass the gate -- fail closed.
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
from bernstein.core.lineage.provenance import (
    PROVENANCE_ARTEFACT_KIND,
    TaintVerificationError,
    TrustClass,
    verify_taint,
)

_OP_SECRET = b"op-secret-xyz"


class _Agent:
    def __init__(self, agent_id: str, kid: str) -> None:
        self.agent_id = agent_id
        self.kid = kid
        self.priv, self.pub = generate_keypair()
        self.card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=self.pub)


def _entry(
    agent: _Agent,
    path: str,
    content_hash: str,
    parents: list[str],
    *,
    kind: str = "file",
    trust: str | None = None,
    ts_ns: int = 1,
) -> LineageEntry:
    unsigned = LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind=kind,
        content_hash=content_hash,
        parent_hashes=parents,
        agent_id=agent.agent_id,
        agent_card_kid=agent.kid,
        tool_call_id="tc",
        span_id="s",
        ts_ns=ts_ns,
        operator_hmac="",
        trust_class=trust,
    )
    op = compute_operator_hmac(unsigned, _OP_SECRET)
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind=kind,
        content_hash=content_hash,
        parent_hashes=parents,
        agent_id=agent.agent_id,
        agent_card_kid=agent.kid,
        tool_call_id="tc",
        span_id="s",
        ts_ns=ts_ns,
        operator_hmac=op,
        trust_class=trust,
    )


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


def _build(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return (log_path, cards_dir, derived_target_hash) for a clean chain."""
    a = _Agent("agent:gw", "k1")
    cards = tmp_path / "agents"
    _write_card(cards, a)
    log = tmp_path / "lineage" / "log.jsonl"
    src = _entry(
        a, "provenance/web/x", "sha256:" + "1" * 64, [], kind=PROVENANCE_ARTEFACT_KIND, trust="public", ts_ns=1
    )
    derived = _entry(a, "src/derived.py", "sha256:" + "2" * 64, [entry_hash(src)], ts_ns=2)
    _write_log_and_sigs(log, [src, derived], a)
    return log, cards, entry_hash(derived)


def test_clean_chain_passes_gate_and_yields_verdict(tmp_path: Path) -> None:
    log, cards, target = _build(tmp_path)
    assert check(log_path=log, agent_cards_dir=cards, operator_secret=_OP_SECRET).ok is True
    verdict = verify_taint(log, cards, target, operator_secret=_OP_SECRET)
    assert verdict.trust is TrustClass.PUBLIC
    assert verdict.tainted is True


def test_mutating_trust_class_fails_gate_and_verification(tmp_path: Path) -> None:
    log, cards, target = _build(tmp_path)
    raw = log.read_text()
    tampered = raw.replace('"trust_class":"public"', '"trust_class":"operator"', 1)
    assert tampered != raw
    log.write_text(tampered)
    # The existing lineage gate rejects the mutated record (broken signature).
    assert check(log_path=log, agent_cards_dir=cards, operator_secret=_OP_SECRET).ok is False
    # And verify_taint refuses to emit a verdict from a failing log.
    with pytest.raises(TaintVerificationError):
        verify_taint(log, cards, target, operator_secret=_OP_SECRET)


def test_reparenting_a_lineage_edge_fails_verification(tmp_path: Path) -> None:
    log, cards, target = _build(tmp_path)
    raw = log.read_text()
    # Point the derived file's parent at a hash that is not in the log.
    forged_parent = "sha256:" + "e" * 64
    lines = raw.splitlines()
    obj = json.loads(lines[1])
    obj["parent_hashes"] = [forged_parent]
    lines[1] = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    log.write_text("\n".join(lines) + "\n")
    assert check(log_path=log, agent_cards_dir=cards, operator_secret=_OP_SECRET).ok is False
    with pytest.raises(TaintVerificationError):
        verify_taint(log, cards, target, operator_secret=_OP_SECRET)
