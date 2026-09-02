"""Tests for ``bernstein lineage sensitivity <artefact>`` (issue #5042).

Slice 3. "This is confidential" invites an argument; "this is confidential
because it derives, through these hops, from that entry" ends it. These tests
pin that the command reports the effective class *and* the walk that produced
it, and that it refuses to report anything at all from a log that fails the
lineage gate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.lineage_cmd import lineage_cmd
from bernstein.core.lineage.entry import (
    LineageEntry,
    canonicalise,
    compute_operator_hmac,
    entry_hash,
)
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached

_OP_SECRET = b"op-secret-xyz"


class _Agent:
    def __init__(self, agent_id: str = "agent:gw", kid: str = "k1") -> None:
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


def _summary_chain(tmp_path: Path) -> tuple[Path, Path, LineageEntry, LineageEntry, LineageEntry]:
    """A confidential document -> an extract -> a summary carrying no label."""
    agent = _Agent()
    cards = tmp_path / "agents"
    _write_card(cards, agent)
    log = tmp_path / "lineage" / "log.jsonl"

    source = _signed_entry(
        agent, "docs/board-minutes.md", "sha256:" + "1" * 64, [], sensitivity="confidential", ts_ns=1
    )
    extract = _signed_entry(agent, "docs/extract.md", "sha256:" + "2" * 64, [entry_hash(source)], ts_ns=2)
    summary = _signed_entry(agent, "docs/summary.md", "sha256:" + "3" * 64, [entry_hash(extract)], ts_ns=3)
    _write_log_and_sigs(log, [source, extract, summary], agent)
    return log, cards, source, extract, summary


def test_cli_reports_the_effective_class_and_the_path_that_produced_it(tmp_path: Path) -> None:
    # The load-bearing one. A summary two hops from a classified document is
    # confidential, and the command says which entry made it so and how the
    # walk reaches it -- not just the class.
    log, cards, source, _extract, _summary = _summary_chain(tmp_path)
    result = CliRunner().invoke(
        lineage_cmd,
        ["sensitivity", "docs/summary.md", "--log", str(log), "--cards", str(cards)],
    )
    assert result.exit_code == 0, result.output
    assert "confidential" in result.output
    # The blamed entry is named, and the walk that reaches it is rendered.
    assert entry_hash(source)[:16] in result.output
    assert "docs/board-minutes.md" in result.output


def test_cli_refuses_to_report_a_verdict_from_a_log_that_fails_the_gate(tmp_path: Path) -> None:
    # A verdict computed from an unverified log is not evidence of anything.
    log, cards, _source, _extract, _summary = _summary_chain(tmp_path)
    lines = log.read_text().splitlines()
    obj = json.loads(lines[2])
    obj["parent_hashes"] = []
    lines[2] = json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    log.write_text("\n".join(lines) + "\n")

    result = CliRunner().invoke(
        lineage_cmd,
        ["sensitivity", "docs/summary.md", "--log", str(log), "--cards", str(cards)],
    )
    assert result.exit_code == 1
    assert "gate" in result.output.lower()
    # No class is printed at all: the command reports a failure, not a verdict.
    assert "confidential" not in result.output


def test_cli_reports_the_fail_closed_default_for_an_unknown_artefact(tmp_path: Path) -> None:
    log, cards, _source, _extract, _summary = _summary_chain(tmp_path)
    result = CliRunner().invoke(
        lineage_cmd,
        ["sensitivity", "docs/never-written.md", "--log", str(log), "--cards", str(cards)],
    )
    assert result.exit_code == 0, result.output
    assert "restricted" in result.output


def test_cli_reports_the_fail_closed_default_when_there_is_no_log(tmp_path: Path) -> None:
    # Absence of evidence is not evidence of harmlessness: the same rule that
    # governs an unlabelled closure governs a missing log.
    result = CliRunner().invoke(
        lineage_cmd,
        [
            "sensitivity",
            "docs/summary.md",
            "--log",
            str(tmp_path / "nowhere" / "log.jsonl"),
            "--cards",
            str(tmp_path / "agents"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "restricted" in result.output


def test_cli_json_output_carries_every_verdict_field(tmp_path: Path) -> None:
    log, cards, source, extract, summary = _summary_chain(tmp_path)
    result = CliRunner().invoke(
        lineage_cmd,
        ["sensitivity", "docs/summary.md", "--log", str(log), "--cards", str(cards), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sensitivity"] == "confidential"
    assert payload["resolved"] is True
    assert payload["raised_by"] == entry_hash(source)
    assert payload["path"] == [entry_hash(summary), entry_hash(extract), entry_hash(source)]
    assert [entry_hash(source), "confidential"] in [list(r) for r in payload["sensitivity_records"]]
    assert set(payload["closure"]) == {entry_hash(source), entry_hash(extract), entry_hash(summary)}


def test_cli_accepts_an_entry_hash_target(tmp_path: Path) -> None:
    log, cards, source, _extract, summary = _summary_chain(tmp_path)
    result = CliRunner().invoke(
        lineage_cmd,
        ["sensitivity", entry_hash(summary), "--log", str(log), "--cards", str(cards), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"] == entry_hash(summary)
    assert payload["raised_by"] == entry_hash(source)
