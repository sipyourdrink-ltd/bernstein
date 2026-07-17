"""CLI tests for ``bernstein mission digest`` (#2510).

Covers show / send / verify end to end against a real mission ledger. The send
path is idempotent per fire; verify recomputes the digest from the ledger and
proves a posted message matches, detecting an edited message as a mismatch.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

import bernstein.cli.commands.mission_cmd as mission_cmd
from bernstein.cli.commands.mission_cmd import mission_group
from bernstein.core.chat.bridge import BridgeProtocol
from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
)
from bernstein.core.orchestration.missions import (
    MissionSpec,
    PhaseSpec,
    define_mission,
    enter_phase,
    gather_evidence_hashes,
    mission_ledger_dir,
    pass_phase,
)
from bernstein.core.persistence.work_ledger import WorkLedger

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32
_FIRE = "1700000000"


class _SpyBridge(BridgeProtocol):
    platform = "spy"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None

    async def send_message(self, thread_id: str, text: str) -> str:
        self.sent.append((thread_id, text))
        return f"msg-{len(self.sent)}"

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:  # pragma: no cover
        return None

    async def push_approval(self, approval: Any) -> str:  # pragma: no cover
        return "a"

    def on_command(self, name: str, handler: Any) -> None:  # pragma: no cover
        return None

    def on_button(self, handler: Any) -> None:  # pragma: no cover
        return None


def _seal_evidence(workdir: Path, task_id: str) -> None:
    priv, pub = load_or_create_evidence_identity(workdir / ".sdd" / "identity")
    outcome = ProducerOutcome(
        producer=EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        exit_code=0,
        output=f"ok {task_id}\n".encode(),
    )
    build_evidence_bundle(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id=task_id,
        outcomes=(outcome,),
        timestamp=1000,
    )


def _build_mission(workdir: Path) -> None:
    sdd_dir = workdir / ".sdd"
    spec = MissionSpec(
        mission_id="m-cli",
        goal="cli mission",
        phases=(PhaseSpec(phase_id="p1", name="prepare", gate=("task-a",), envelope="mission-m-cli-p1", budget_usd=30.0),),
    )
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(workdir, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    pass_phase(
        ledger=ledger,
        spec=spec,
        phase_id="p1",
        evidence_hashes=gather_evidence_hashes(workdir, ("task-a",)),
        spend_usd=10.0,
    )
    ledger.close()


def test_digest_show_json(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    runner = CliRunner()
    res = runner.invoke(mission_group, ["digest", "show", "m-cli", "--fire-time", _FIRE, "--workdir", str(tmp_path), "--json"])
    assert res.exit_code == 0, res.output
    body = json.loads(res.output)
    assert body["mission_id"] == "m-cli"
    assert body["digest_hash"]
    assert body["digest_hash"] in body["message"]
    assert body["receipt_id"].startswith("missiondigest-")


def test_digest_send_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_mission(tmp_path)
    spy = _SpyBridge()
    monkeypatch.setattr(mission_cmd, "_build_bridge", lambda platform, token: spy)
    runner = CliRunner()

    args = [
        "digest", "send", "m-cli",
        "--fire-time", _FIRE,
        "--platform", "slack",
        "--thread", "C123",
        "--token", "xoxb-test",
        "--workdir", str(tmp_path),
        "--json",
    ]
    first = runner.invoke(mission_group, args)
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["posted"] is True

    second = runner.invoke(mission_group, args)
    assert second.exit_code == 0, second.output
    body = json.loads(second.output)
    assert body["posted"] is False
    assert body["reason"] == "already_delivered"

    assert len(spy.sent) == 1  # no double-post across two fires


def test_digest_verify_matches_then_detects_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_mission(tmp_path)
    spy = _SpyBridge()
    monkeypatch.setattr(mission_cmd, "_build_bridge", lambda platform, token: spy)
    runner = CliRunner()

    send = runner.invoke(
        mission_group,
        [
            "digest", "send", "m-cli",
            "--fire-time", _FIRE,
            "--platform", "telegram",
            "--thread", "42",
            "--token", "tg-test",
            "--workdir", str(tmp_path),
            "--json",
        ],
    )
    assert send.exit_code == 0, send.output
    posted_text = spy.sent[0][1]

    # Genuine posted message verifies (message-side + receipt-side proof).
    msg_ok = tmp_path / "posted_ok.txt"
    msg_ok.write_text(posted_text, encoding="utf-8")
    ok = runner.invoke(
        mission_group,
        ["digest", "verify", "m-cli", "--fire-time", _FIRE, "--message", str(msg_ok), "--workdir", str(tmp_path), "--json"],
    )
    assert ok.exit_code == 0, ok.output
    report = json.loads(ok.output)
    assert report["ok"] is True
    assert report["message_matches"] is True
    assert report["chain_verified"] is True
    assert report["receipt_present"] is True

    # An edited message is detected as a mismatch (exit 2).
    msg_bad = tmp_path / "posted_bad.txt"
    msg_bad.write_text(posted_text.replace("$10.00", "$88.00"), encoding="utf-8")
    bad = runner.invoke(
        mission_group,
        ["digest", "verify", "m-cli", "--fire-time", _FIRE, "--message", str(msg_bad), "--workdir", str(tmp_path), "--json"],
    )
    assert bad.exit_code == 2, bad.output
    assert json.loads(bad.output)["message_matches"] is False
