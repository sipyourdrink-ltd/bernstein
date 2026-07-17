"""Tests for ``bernstein audit taint`` -- offline recompute plus verdict.

Two independent verifiers recompute byte-identical verdicts from the same
log with no live process. The command runs the lineage gate first, so a
tampered provenance record surfaces as a non-zero exit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.lineage.entry import (
    LineageEntry,
    canonicalise,
    compute_operator_hmac,
    entry_hash,
)
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached
from bernstein.core.lineage.provenance import PROVENANCE_ARTEFACT_KIND

_OP_SECRET = b"op-secret-cli"


def _agent() -> tuple[AgentCard, str, str, str]:
    priv, pub = generate_keypair()
    return AgentCard(agent_id="agent:gw", kid="k1", public_key_pem=pub), priv, "agent:gw", "k1"


def _entry(
    path: str, ch: str, parents: list[str], agent_id: str, kid: str, *, kind: str, trust: str | None, ts: int
) -> LineageEntry:
    unsigned = LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind=kind,
        content_hash=ch,
        parent_hashes=parents,
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc",
        span_id="s",
        ts_ns=ts,
        operator_hmac="",
        trust_class=trust,
    )
    op = compute_operator_hmac(unsigned, _OP_SECRET)
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind=kind,
        content_hash=ch,
        parent_hashes=parents,
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc",
        span_id="s",
        ts_ns=ts,
        operator_hmac=op,
        trust_class=trust,
    )


def _materialize(tmp_path: Path) -> tuple[Path, Path, str]:
    card, priv, agent_id, kid = _agent()
    cards = tmp_path / "agents"
    d = cards / agent_id
    d.mkdir(parents=True)
    (d / "card.json").write_text(
        json.dumps(
            {"protocolVersion": "a2a/1.0", "agent_id": agent_id, "kid": kid, "public_key_pem": card.public_key_pem}
        )
    )
    log = tmp_path / "lineage" / "log.jsonl"
    log.parent.mkdir(parents=True)
    src = _entry(
        "provenance/web/x", "sha256:" + "1" * 64, [], agent_id, kid, kind=PROVENANCE_ARTEFACT_KIND, trust="public", ts=1
    )
    derived = _entry(
        "src/derived.py", "sha256:" + "2" * 64, [entry_hash(src)], agent_id, kid, kind="file", trust=None, ts=2
    )
    sig_root = log.parent / "signatures"
    with log.open("wb") as f:
        for e in (src, derived):
            f.write(canonicalise(e) + b"\n")
    for e in (src, derived):
        jws = sign_detached(canonicalise(e), priv, kid=kid)
        ph = hashlib.sha256(e.artefact_path.encode()).hexdigest()
        dest = sig_root / ph[:2] / ph
        dest.mkdir(parents=True, exist_ok=True)
        (dest / (entry_hash(e).replace("sha256:", "") + ".jws")).write_text(jws)
    return log, cards, "src/derived.py"


def test_taint_reports_untrusted_verdict(tmp_path: Path, monkeypatch) -> None:
    log, cards, target = _materialize(tmp_path)
    monkeypatch.setenv("BERNSTEIN_LINEAGE_OP_SECRET", _OP_SECRET.decode())
    result = CliRunner().invoke(
        audit_group,
        ["taint", target, "--log", str(log), "--cards", str(cards), "--json"],
    )
    assert result.exit_code == 1, result.output  # tainted -> non-zero
    payload = json.loads(result.output)
    assert payload["trust"] == "public"
    assert payload["tainted"] is True
    assert payload["resolved"] is True


def test_taint_verdict_is_reproducible(tmp_path: Path, monkeypatch) -> None:
    log, cards, target = _materialize(tmp_path)
    monkeypatch.setenv("BERNSTEIN_LINEAGE_OP_SECRET", _OP_SECRET.decode())
    runner = CliRunner()
    args = ["taint", target, "--log", str(log), "--cards", str(cards), "--json"]
    out1 = runner.invoke(audit_group, args).output
    out2 = runner.invoke(audit_group, args).output
    assert out1 == out2  # byte-identical offline recompute


def test_taint_fails_on_tampered_record(tmp_path: Path, monkeypatch) -> None:
    log, cards, target = _materialize(tmp_path)
    monkeypatch.setenv("BERNSTEIN_LINEAGE_OP_SECRET", _OP_SECRET.decode())
    raw = log.read_text()
    log.write_text(raw.replace('"trust_class":"public"', '"trust_class":"operator"', 1))
    result = CliRunner().invoke(
        audit_group,
        ["taint", target, "--log", str(log), "--cards", str(cards)],
    )
    assert result.exit_code != 0
    assert "verif" in result.output.lower() or "tamper" in result.output.lower() or "gate" in result.output.lower()
