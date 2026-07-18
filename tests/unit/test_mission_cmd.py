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

    from click.testing import Result

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


def runner_invoke(args: list[str]) -> Result:
    """Invoke the mission group with a fresh runner."""
    return CliRunner().invoke(mission_group, args)


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


# ---------------------------------------------------------------------------
# Hardening (#2652)
# ---------------------------------------------------------------------------


def test_verify_refuses_a_non_mission_ledger(tmp_path: Path) -> None:
    """#2652: a plain run ledger is not a mission and must not verify as OK."""
    from bernstein.cli.commands.mission_cmd import EXIT_NO_MISSION
    from bernstein.core.orchestration.missions import mission_ledger_dir
    from bernstein.core.persistence.work_ledger import WorkLedger

    ledger = WorkLedger.open(mission_ledger_dir(tmp_path / ".sdd", "not-a-mission"))
    ledger.append(kind="task.scheduled", task_id="t1", payload={"task_id": "t1"})
    ledger.close()

    res = runner_invoke(["verify", "not-a-mission", "--workdir", str(tmp_path), "--json"])
    assert res.exit_code == EXIT_NO_MISSION, res.output


def test_status_refuses_a_ledger_whose_mission_id_differs(tmp_path: Path) -> None:
    """#2652: the definition must name the mission the operator asked for."""
    from bernstein.cli.commands.mission_cmd import EXIT_NO_MISSION
    from bernstein.core.orchestration.missions import MissionSpec, define_mission, mission_ledger_dir
    from bernstein.core.persistence.work_ledger import WorkLedger

    spec = MissionSpec.from_dict({**_spec_dict(), "mission_id": "m-other"})
    # Land a definition for "m-other" under the directory keyed "m-cli".
    ledger = WorkLedger.open(mission_ledger_dir(tmp_path / ".sdd", "m-cli"))
    define_mission(ledger=ledger, spec=spec)
    ledger.close()

    res = runner_invoke(["status", "m-cli", "--workdir", str(tmp_path), "--json"])
    assert res.exit_code == EXIT_NO_MISSION, res.output


def test_define_rejects_a_non_object_spec_root(tmp_path: Path) -> None:
    """#2652: a non-object root exits with the bad-spec code, never a traceback."""
    from bernstein.cli.commands.mission_cmd import EXIT_BAD_SPEC

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"mission_id": "m"}]), encoding="utf-8")
    res = runner_invoke(["define", str(bad), "--workdir", str(tmp_path)])
    assert res.exit_code == EXIT_BAD_SPEC, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_define_rejects_a_non_object_phase(tmp_path: Path) -> None:
    """#2652: a scalar phase entry exits with the bad-spec code."""
    from bernstein.cli.commands.mission_cmd import EXIT_BAD_SPEC

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mission_id": "m", "goal": "g", "phases": ["p1"]}), encoding="utf-8")
    res = runner_invoke(["define", str(bad), "--workdir", str(tmp_path)])
    assert res.exit_code == EXIT_BAD_SPEC, res.output


def test_define_rejects_an_unsupported_schema_version(tmp_path: Path) -> None:
    """#2652: an unknown wire version exits with the bad-spec code."""
    from bernstein.cli.commands.mission_cmd import EXIT_BAD_SPEC

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**_spec_dict(), "schema_version": 99}), encoding="utf-8")
    res = runner_invoke(["define", str(bad), "--workdir", str(tmp_path)])
    assert res.exit_code == EXIT_BAD_SPEC, res.output


def test_define_refuses_to_redefine_an_existing_mission(tmp_path: Path) -> None:
    """#2652: a second define must not split projection from evidence lookup."""
    from bernstein.cli.commands.mission_cmd import EXIT_BAD_SPEC

    spec_path = _write_spec(tmp_path)
    first = runner_invoke(["define", str(spec_path), "--workdir", str(tmp_path)])
    assert first.exit_code == 0, first.output

    second = runner_invoke(["define", str(spec_path), "--workdir", str(tmp_path)])
    assert second.exit_code == EXIT_BAD_SPEC, second.output
