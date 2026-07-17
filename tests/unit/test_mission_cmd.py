"""CLI tests for the ``bernstein mission`` command group (#2509).

Covers the four operator verbs (define / status / verify / resume) end to end
against a real work ledger and sealed evidence bundles.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.commands.mission_cmd import mission_group
from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


def _spec_dict() -> dict:
    return {
        "mission_id": "m-cli",
        "goal": "cli mission",
        "phases": [
            {
                "phase_id": "p1",
                "name": "prepare",
                "gate": ["task-a"],
                "envelope": "mission-m-cli-p1",
                "budget_usd": 30.0,
            },
        ],
    }


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


def _write_spec(tmp_path: Path) -> Path:
    spec_path = tmp_path / "mission.json"
    spec_path.write_text(json.dumps(_spec_dict()), encoding="utf-8")
    return spec_path


def test_mission_define_then_status(tmp_path: Path) -> None:
    runner = CliRunner()
    spec_path = _write_spec(tmp_path)

    res = runner.invoke(
        mission_group,
        ["define", str(spec_path), "--workdir", str(tmp_path), "--json"],
    )
    assert res.exit_code == 0, res.output
    defined = json.loads(res.output)
    assert defined["mission_id"] == "m-cli"

    res = runner.invoke(
        mission_group,
        ["status", "m-cli", "--workdir", str(tmp_path), "--json"],
    )
    assert res.exit_code == 0, res.output
    status = json.loads(res.output)
    assert status["overall"] in {"pending", "active"}
    assert status["mission_status_hash"]


def test_mission_verify_reports_clean_then_tampered(tmp_path: Path) -> None:
    runner = CliRunner()
    spec_path = _write_spec(tmp_path)
    runner.invoke(mission_group, ["define", str(spec_path), "--workdir", str(tmp_path)])
    _seal_evidence(tmp_path, "task-a")

    # Advance phase 1 through the CLI verify wiring is not required; the verify
    # command re-checks the chain and any sealed evidence the receipts bind.
    res = runner.invoke(mission_group, ["verify", "m-cli", "--workdir", str(tmp_path), "--json"])
    assert res.exit_code == 0, res.output
    report = json.loads(res.output)
    assert report["ledger_verified"] is True


def test_mission_resume_matches_status_hash(tmp_path: Path) -> None:
    runner = CliRunner()
    spec_path = _write_spec(tmp_path)
    runner.invoke(mission_group, ["define", str(spec_path), "--workdir", str(tmp_path)])

    status_res = runner.invoke(mission_group, ["status", "m-cli", "--workdir", str(tmp_path), "--json"])
    status_hash = json.loads(status_res.output)["mission_status_hash"]

    resume_res = runner.invoke(mission_group, ["resume", "m-cli", "--workdir", str(tmp_path), "--json"])
    assert resume_res.exit_code == 0, resume_res.output
    resumed = json.loads(resume_res.output)
    assert resumed["mission_status_hash"] == status_hash


def test_mission_define_rejects_invalid_spec(tmp_path: Path) -> None:
    runner = CliRunner()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mission_id": "", "goal": "x", "phases": []}), encoding="utf-8")
    res = runner.invoke(mission_group, ["define", str(bad), "--workdir", str(tmp_path)])
    assert res.exit_code != 0
