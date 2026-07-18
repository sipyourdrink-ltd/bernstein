"""Unit tests for the mission timeline API routes (#2510).

The endpoints are a read-only projection surface over the mission's work-ledger
chain: no mission-side state, no writes. When chain verification fails the
projection payload marks the mission unverified so the screen switches to an
explicit unverified banner instead of best-effort rendering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
)
from bernstein.core.orchestration.mission_digest import build_mission_digest, render_digest_message
from bernstein.core.orchestration.missions import (
    MissionSpec,
    PhaseSpec,
    define_mission,
    enter_phase,
    gather_evidence_hashes,
    mission_ledger_dir,
    pass_phase,
    project_mission_from_ledger,
)
from bernstein.core.persistence.work_ledger import WorkLedger
from bernstein.core.routes.missions import router

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32
_FIRE = 1_700_000_000


def _make_app(workdir: Path) -> FastAPI:
    app = FastAPI()
    app.state.workdir = workdir
    app.state.sdd_dir = workdir / ".sdd"
    app.include_router(router)
    return app


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


def _build_mission(workdir: Path) -> MissionSpec:
    sdd_dir = workdir / ".sdd"
    spec = MissionSpec(
        mission_id="m-1",
        goal="ship the migration",
        phases=(
            PhaseSpec(phase_id="p1", name="prepare", gate=("task-a",), envelope="mission-m-1-p1", budget_usd=40.0),
            PhaseSpec(phase_id="p2", name="migrate", gate=("task-b",), envelope="mission-m-1-p2", budget_usd=25.0),
        ),
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
        spend_usd=12.0,
    )
    ledger.close()
    return spec


# ---------------------------------------------------------------------------
# Listing + projection
# ---------------------------------------------------------------------------


def test_list_missions_empty(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    response = client.get("/missions")
    assert response.status_code == 200
    assert response.json() == {"missions": []}


def test_list_missions_returns_ledger_backed_missions(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))
    response = client.get("/missions")
    assert response.status_code == 200
    assert response.json() == {"missions": ["m-1"]}


def test_projection_endpoint_folds_ledger(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))

    response = client.get("/missions/m-1")
    assert response.status_code == 200
    body = response.json()

    expected = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    assert body["mission_status_hash"] == expected.status_hash
    assert body["ledger_verified"] is True
    assert body["status"]["overall"] == "active"
    # Every phase carries its provenance handles (receipt hash + evidence).
    phases = body["status"]["phases"]
    assert phases[0]["receipt_hash"]
    assert phases[0]["evidence_bundle_hashes"]


def test_unknown_mission_is_404(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/does-not-exist").status_code == 404


def test_invalid_mission_id_is_400(tmp_path: Path) -> None:
    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/../etc").status_code in (400, 404)


# ---------------------------------------------------------------------------
# Unverified banner: a tampered ledger flips the projection to unverified
# ---------------------------------------------------------------------------


def test_tampered_ledger_projects_unverified(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    bucket = mission_ledger_dir(tmp_path / ".sdd", "m-1") / "000000.jsonl"
    raw = bucket.read_text(encoding="utf-8")
    # Flip a spend figure inside a chained entry: the recomputed hash no longer
    # matches, so the chain does not verify.
    tampered = raw.replace('"spend_usd":12.0', '"spend_usd":999.0')
    assert tampered != raw
    bucket.write_text(tampered, encoding="utf-8")

    client = TestClient(_make_app(tmp_path))
    body = client.get("/missions/m-1").json()
    assert body["ledger_verified"] is False
    assert body["status"]["overall"] == "unverified"


# ---------------------------------------------------------------------------
# Digest endpoint: read-only recompute matching the pure fold
# ---------------------------------------------------------------------------


def test_digest_endpoint_matches_pure_fold(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))

    response = client.get("/missions/m-1/digest", params={"fire_time": _FIRE})
    assert response.status_code == 200
    body = response.json()

    proj = project_mission_from_ledger(sdd_dir=tmp_path / ".sdd", workdir=tmp_path, mission_id="m-1")
    digest = build_mission_digest(proj, fire_time=_FIRE)
    assert body["digest_hash"] == digest.digest_hash()
    assert body["receipt_id"] == digest.receipt_id()
    assert body["message"] == render_digest_message(digest)
    assert digest.digest_hash() in body["message"]


def test_digest_endpoint_requires_fire_time(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))
    # Missing fire_time -> 422 (FastAPI query validation).
    assert client.get("/missions/m-1/digest").status_code == 422


# ---------------------------------------------------------------------------
# Evidence provenance link
# ---------------------------------------------------------------------------


def test_evidence_endpoint_serves_bundle(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))
    response = client.get("/missions/m-1/evidence/task-a")
    assert response.status_code == 200
    body = response.json()
    assert body["bundle_hash"]
    assert body["gate_passed"] is True


def test_evidence_endpoint_404_for_missing_bundle(tmp_path: Path) -> None:
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/m-1/evidence/task-z").status_code == 404


# ---------------------------------------------------------------------------
# Review hardening (#2680) -- the route guards the same sink as the CLI
# ---------------------------------------------------------------------------


def test_route_refuses_a_non_mission_ledger(tmp_path: Path) -> None:
    """#2680: a plain run ledger must not be served as a healthy mission.

    Missions share the ledger root with run ledgers, so directory existence
    proves nothing. Without a definition the projection renders an empty
    pending mission with ledger_verified true -- a non-mission served as fine.
    The CLI already refuses this input; the route reaches the same sink and
    has to agree.
    """
    from bernstein.core.orchestration.missions import mission_ledger_dir
    from bernstein.core.persistence.work_ledger import WorkLedger

    ledger = WorkLedger.open(mission_ledger_dir(tmp_path / ".sdd", "plain-run"))
    ledger.append(kind="task.scheduled", task_id="t1", payload={"task_id": "t1"})
    ledger.close()

    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/plain-run").status_code == 404


def test_route_refuses_a_ledger_declaring_a_different_mission(tmp_path: Path) -> None:
    """#2680: an intact ledger holding another mission is a genuine not-found."""
    from bernstein.core.orchestration.missions import (
        MissionSpec,
        PhaseSpec,
        define_mission,
        mission_ledger_dir,
    )
    from bernstein.core.persistence.work_ledger import WorkLedger

    spec = MissionSpec(
        mission_id="m-other",
        goal="g",
        phases=(PhaseSpec(phase_id="p1", name="p", gate=("t",), envelope="e", budget_usd=1.0),),
    )
    ledger = WorkLedger.open(mission_ledger_dir(tmp_path / ".sdd", "m-cli"))
    define_mission(ledger=ledger, spec=spec)
    ledger.close()

    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/m-cli").status_code == 404


def test_route_digest_and_evidence_also_refuse_a_non_mission_ledger(tmp_path: Path) -> None:
    """#2680: all three guarded endpoints agree, not just the projection one."""
    from bernstein.core.orchestration.missions import mission_ledger_dir
    from bernstein.core.persistence.work_ledger import WorkLedger

    ledger = WorkLedger.open(mission_ledger_dir(tmp_path / ".sdd", "plain-run"))
    ledger.append(kind="task.scheduled", task_id="t1", payload={"task_id": "t1"})
    ledger.close()

    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/plain-run/digest?fire_time=1750000000").status_code == 404
    assert client.get("/missions/plain-run/evidence/task-a").status_code == 404


def test_route_still_serves_a_real_mission(tmp_path: Path) -> None:
    """Guard: the new check must not reject the honest case."""
    _build_mission(tmp_path)
    client = TestClient(_make_app(tmp_path))
    assert client.get("/missions/m-1").status_code == 200


def test_route_renders_a_tampered_mission_id_as_unverified(tmp_path: Path) -> None:
    """#2680: a tampered ledger must not be downgraded to 404 not-found.

    The declared id comes from a row no hash has checked, so refusing on it
    turns the loudest signal available (this chain does not verify) into the
    quietest (there is no such mission), which reads as an innocent typo. Only
    a chain that verifies may answer not-found. The shipped behaviour is a 200
    carrying the unverified verdict, and the timeline docs promise exactly that
    banner.
    """
    import json as _json

    from bernstein.core.orchestration.missions import mission_ledger_dir

    _build_mission(tmp_path)
    bucket = mission_ledger_dir(tmp_path / ".sdd", "m-1") / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    row = _json.loads(lines[0])
    row["payload"]["mission_id"] = "m-evil"
    lines[0] = _json.dumps(row, separators=(",", ":"))
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

    client = TestClient(_make_app(tmp_path))
    response = client.get("/missions/m-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ledger_verified"] is False
    assert body["status"]["overall"] == "unverified"


def test_route_renders_an_ambiguous_double_definition_as_unverified(tmp_path: Path) -> None:
    """#2680: two definitions is an integrity failure, not an absence."""
    from bernstein.core.orchestration.missions import mission_ledger_dir
    from bernstein.core.persistence.work_ledger import KIND_MISSION_DEFINED, WorkLedger

    _build_mission(tmp_path)
    ledger = WorkLedger.open(mission_ledger_dir(tmp_path / ".sdd", "m-1"))
    ledger.append(kind=KIND_MISSION_DEFINED, task_id="", payload={"mission_id": "m-1", "phases": []})
    ledger.close()

    client = TestClient(_make_app(tmp_path))
    response = client.get("/missions/m-1")
    assert response.status_code == 200, response.text
    assert response.json()["status"]["overall"] == "unverified"
