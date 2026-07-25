"""The per-artifact health verdict is a deterministic projection (#2559).

The verdict has to be recomputable by a third party from local state alone,
which means it must be a pure function of what was read off disk and of the
evaluation instant. These tests pin that:

* the same state and the same ``at`` give byte-identical JSON, every time;
* a byte flip anywhere in a spine row carrying the artifact turns it red and
  names the offending entry;
* a leg with nothing to say reports ``not_applicable`` and never drags the
  verdict down -- absence of a signal is not a negative signal;
* a tampered entry belonging to a *different* artifact does not turn this one
  red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.artifact_health import (
    AMBER,
    GREEN,
    LEG_FAIL,
    LEG_NOT_APPLICABLE,
    LEG_PASS,
    LEG_STALE,
    RED,
    ArtifactProduction,
    ArtifactState,
    artifact_health_json,
    artifact_log,
    artifact_log_json,
    collect_artifact_state,
    compute_artifact_health,
    list_artifact_keys,
)
from bernstein.core.lineage.spine import LineageSpine, SpineEntry

_KEY = b"h" * 32
_URI = "pkg://pypi/bernstein/3.9.0"


def _spine(workdir: Path, run_id: str = "run-1") -> LineageSpine:
    return LineageSpine(workdir / ".sdd" / "lineage", run_id=run_id, hmac_key=_KEY)


def _record(
    workdir: Path,
    uri: str,
    content: bytes,
    *,
    ts: int,
    run_id: str = "run-1",
    actor: str = "agent-release",
    model: str = "claude-opus-5",
) -> SpineEntry:
    return _spine(workdir, run_id).record_entry(
        artifact_path=uri,
        content=content,
        actor=actor,
        step_id="publish",
        model=model,
        timestamp=ts,
    )


def _leg(verdict_json: str, name: str) -> dict[str, str]:
    legs = json.loads(verdict_json)["legs"]
    return next(leg for leg in legs if leg["name"] == name)


# ---------------------------------------------------------------------------
# Determinism (AC1)
# ---------------------------------------------------------------------------


def test_same_state_and_instant_give_byte_identical_json(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    first = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    second = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert first == second


def test_verdict_is_independent_of_the_instant_when_no_cadence_is_declared(tmp_path: Path) -> None:
    """With no cadence, ``at`` only shows up as the recorded evaluation stamp."""
    _record(tmp_path, _URI, b"wheel", ts=100)
    early = json.loads(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=101))
    late = json.loads(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=10_000_000))
    early.pop("evaluated_at")
    late.pop("evaluated_at")
    assert early == late


def test_verdict_document_is_canonical_json(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert payload == json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_collected_state_is_read_once_and_frozen(tmp_path: Path) -> None:
    """A verdict is a function of the snapshot, not of a moving filesystem."""
    _record(tmp_path, _URI, b"wheel", ts=100)
    state = collect_artifact_state(tmp_path, _URI, hmac_key=_KEY)
    _record(tmp_path, _URI, b"newer-wheel", ts=200)
    # The already-collected state still yields its original verdict.
    assert compute_artifact_health(state, at=500).production_count == 1
    with pytest.raises(AttributeError):
        state.uri = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Green path
# ---------------------------------------------------------------------------


def test_a_single_intact_production_is_green(tmp_path: Path) -> None:
    entry = _record(tmp_path, _URI, b"wheel", ts=100)
    verdict = json.loads(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500))
    assert verdict["verdict"] == GREEN
    assert verdict["tip"]["entry_hash"] == entry.entry_hash
    assert verdict["tip"]["actor"] == "agent-release"
    assert verdict["tip"]["model"] == "claude-opus-5"
    assert verdict["last_produced_at"] == 100


def test_absent_signals_report_not_applicable(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert _leg(payload, "evidence")["status"] == LEG_NOT_APPLICABLE
    assert _leg(payload, "cadence")["status"] == LEG_NOT_APPLICABLE
    assert json.loads(payload)["verdict"] == GREEN


def test_repeated_productions_over_time_stay_green(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"v1", ts=100)
    _record(tmp_path, _URI, b"v2", ts=200)
    _record(tmp_path, _URI, b"v3", ts=300)
    verdict = json.loads(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=400))
    assert verdict["verdict"] == GREEN
    assert verdict["production_count"] == 3
    assert verdict["last_produced_at"] == 300


# ---------------------------------------------------------------------------
# Red path (AC2)
# ---------------------------------------------------------------------------


def test_never_produced_is_red(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert json.loads(payload)["verdict"] == RED
    assert _leg(payload, "produced")["status"] == LEG_FAIL


@pytest.mark.parametrize("field", ["content_hash", "actor", "model", "timestamp", "hmac"])
def test_single_byte_flip_turns_the_verdict_red(tmp_path: Path, field: str) -> None:
    entry = _record(tmp_path, _URI, b"wheel", ts=100)
    spine_path = tmp_path / ".sdd" / "lineage" / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row[field] = row[field] + 1 if isinstance(row[field], int) else row[field][:-1] + "0"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")

    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert json.loads(payload)["verdict"] == RED
    integrity = _leg(payload, "chain_integrity")
    assert integrity["status"] == LEG_FAIL
    assert entry.entry_hash in integrity["detail"] or "chain verification failed" in integrity["detail"]


def test_a_tampered_neighbour_artifact_does_not_turn_this_one_red(tmp_path: Path) -> None:
    """Per-entry verification keeps one bad row from condemning the whole run."""
    _record(tmp_path, _URI, b"wheel", ts=100)
    _record(tmp_path, "docs/other.md", b"unrelated", ts=200)

    spine_path = tmp_path / ".sdd" / "lineage" / "run-1" / "spine.jsonl"
    rows = [json.loads(line) for line in spine_path.read_bytes().strip().split(b"\n")]
    rows[1]["actor"] = "impostor"
    spine_path.write_bytes(
        b"".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n" for r in rows
        )
    )

    # The tampered artifact is red ...
    assert json.loads(artifact_health_json(tmp_path, "docs/other.md", hmac_key=_KEY, at=500))["verdict"] == RED
    # ... and the untouched one keeps its own per-entry verdict.
    healthy = json.loads(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500))
    assert _leg(json.dumps(healthy), "produced")["status"] == LEG_PASS


def test_two_sets_of_bytes_claiming_to_be_current_is_red(tmp_path: Path) -> None:
    """A fork: two runs both say they produced the newest version of one key."""
    _record(tmp_path, _URI, b"wheel-a", ts=100, run_id="run-a")
    _record(tmp_path, _URI, b"wheel-b", ts=100, run_id="run-b")
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert json.loads(payload)["verdict"] == RED
    assert _leg(payload, "single_open_tip")["status"] == LEG_FAIL


def test_idempotent_reproduction_of_the_same_bytes_is_not_a_fork(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"same-wheel", ts=100, run_id="run-a")
    _record(tmp_path, _URI, b"same-wheel", ts=100, run_id="run-b")
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert _leg(payload, "single_open_tip")["status"] == LEG_PASS
    assert json.loads(payload)["verdict"] == GREEN


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def test_production_inside_the_cadence_passes(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=150, cadence_seconds=100)
    assert _leg(payload, "cadence")["status"] == LEG_PASS
    assert json.loads(payload)["verdict"] == GREEN


def test_a_lapsed_cadence_is_amber_not_red(tmp_path: Path) -> None:
    """Out of date is not the same as broken."""
    _record(tmp_path, _URI, b"wheel", ts=100)
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=100_000, cadence_seconds=100)
    assert _leg(payload, "cadence")["status"] == LEG_STALE
    assert json.loads(payload)["verdict"] == AMBER


def test_integrity_failure_outranks_a_lapsed_cadence(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    spine_path = tmp_path / ".sdd" / "lineage" / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["actor"] = "impostor"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=100_000, cadence_seconds=100)
    assert json.loads(payload)["verdict"] == RED


def test_a_non_positive_cadence_is_treated_as_undeclared(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=100_000, cadence_seconds=0)
    assert _leg(payload, "cadence")["status"] == LEG_NOT_APPLICABLE


# ---------------------------------------------------------------------------
# Pure-function level
# ---------------------------------------------------------------------------


def _production(entry_hash: str, *, ts: int = 1, verified: bool = True, content: str = "c") -> ArtifactProduction:
    return ArtifactProduction(
        run_id="r",
        entry_hash=entry_hash,
        content_hash=content,
        actor="a",
        model="m",
        step_id="s",
        timestamp=ts,
        verified=verified,
    )


def test_compute_is_pure_over_its_arguments() -> None:
    state = ArtifactState(uri=_URI, productions=(_production("h1"),))
    assert compute_artifact_health(state, at=9).to_dict() == compute_artifact_health(state, at=9).to_dict()


def test_declared_evidence_failure_is_red() -> None:
    state = ArtifactState(
        uri=_URI,
        productions=(_production("h1"),),
        evidence_task_id="T-1",
        evidence_verified=False,
        evidence_detail="signature did not verify",
    )
    health = compute_artifact_health(state, at=9)
    assert health.verdict == RED
    assert any(leg.name == "evidence" and leg.status == LEG_FAIL for leg in health.legs)


def test_declared_evidence_success_is_green() -> None:
    state = ArtifactState(uri=_URI, productions=(_production("h1"),), evidence_task_id="T-1", evidence_verified=True)
    assert compute_artifact_health(state, at=9).verdict == GREEN


def test_ordering_breaks_ties_on_entry_hash_so_the_tip_is_unambiguous() -> None:
    state = ArtifactState(
        uri=_URI,
        productions=(
            _production("sha256:bbb", ts=5, content="same"),
            _production("sha256:aaa", ts=5, content="same"),
        ),
    )
    assert compute_artifact_health(state, at=9).tip_entry_hash == "sha256:bbb"


# ---------------------------------------------------------------------------
# The evidence leg reads real sealed bundles
# ---------------------------------------------------------------------------


def _seal_bundle(workdir: Path, task_id: str, *, declared_and_produced: tuple[str, ...], ts: int) -> None:
    """Seal a real evidence bundle whose signed binding cites ``declared_and_produced``."""
    from bernstein.core.evidence.bundle import run_evidence_gate
    from bernstein.core.evidence.output_diff import OutputDiff

    run_evidence_gate(
        workdir=workdir,
        task_id=task_id,
        producers=[],
        timestamp=ts,
        hmac_key=_KEY,
        output_diff=OutputDiff(declared_and_produced=declared_and_produced),
    )


def test_a_verifying_bundle_that_declares_the_artifact_passes_the_leg(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    _seal_bundle(tmp_path, "T-release", declared_and_produced=(_URI,), ts=100)

    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    evidence = _leg(payload, "evidence")
    assert evidence["status"] == LEG_PASS
    assert "T-release" in evidence["detail"]
    assert json.loads(payload)["verdict"] == GREEN


def test_a_bundle_that_does_not_declare_the_artifact_is_ignored(tmp_path: Path) -> None:
    """Association runs through the bundle's *signed* binding, not proximity."""
    _record(tmp_path, _URI, b"wheel", ts=100)
    _seal_bundle(tmp_path, "T-other", declared_and_produced=("dist/unrelated.whl",), ts=100)

    assert _leg(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500), "evidence")["status"] == (
        LEG_NOT_APPLICABLE
    )


def test_a_tampered_bundle_fails_the_leg_and_turns_the_verdict_red(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    _seal_bundle(tmp_path, "T-release", declared_and_produced=(_URI,), ts=100)

    from bernstein.core.evidence.bundle import bundle_path

    path = bundle_path(tmp_path, "T-release")
    row = json.loads(path.read_bytes())
    row["gate_passed"] = not row["gate_passed"]
    path.write_bytes(json.dumps(row).encode())

    payload = artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500)
    assert _leg(payload, "evidence")["status"] == LEG_FAIL
    assert json.loads(payload)["verdict"] == RED


def test_the_newest_declaring_bundle_wins(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    _seal_bundle(tmp_path, "T-old", declared_and_produced=(_URI,), ts=100)
    _seal_bundle(tmp_path, "T-new", declared_and_produced=(_URI,), ts=200)

    assert "T-new" in _leg(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500), "evidence")["detail"]


def test_a_malformed_bundle_does_not_crash_the_projection(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"wheel", ts=100)
    bundles = tmp_path / ".sdd" / "evidence" / "bundles"
    bundles.mkdir(parents=True)
    (bundles / "T-corrupt.json").write_bytes(b"{not json")
    assert json.loads(artifact_health_json(tmp_path, _URI, hmac_key=_KEY, at=500))["verdict"] == GREEN


# ---------------------------------------------------------------------------
# Attribution (AC5) and listing
# ---------------------------------------------------------------------------


def test_log_answers_who_produced_the_current_tip(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"v1", ts=100, actor="agent-old", model="model-old")
    newest = _record(tmp_path, _URI, b"v2", ts=200, actor="agent-new", model="model-new")

    records = artifact_log(tmp_path, _URI, hmac_key=_KEY)
    assert records[0].entry_hash == newest.entry_hash
    assert records[0].actor == "agent-new"
    assert records[0].model == "model-new"
    assert records[0].verified is True
    assert [r.actor for r in records] == ["agent-new", "agent-old"]


def test_log_spans_runs(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"v1", ts=100, run_id="run-a", actor="agent-a")
    _record(tmp_path, _URI, b"v2", ts=200, run_id="run-b", actor="agent-b")
    assert [r.run_id for r in artifact_log(tmp_path, _URI, hmac_key=_KEY)] == ["run-b", "run-a"]


def test_log_marks_a_tampered_production_rather_than_hiding_it(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"v1", ts=100)
    spine_path = tmp_path / ".sdd" / "lineage" / "run-1" / "spine.jsonl"
    row = json.loads(spine_path.read_bytes().strip())
    row["model"] = "a-model-nobody-ran"
    spine_path.write_bytes(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n")
    records = artifact_log(tmp_path, _URI, hmac_key=_KEY)
    assert len(records) == 1
    assert records[0].verified is False


def test_log_limit_is_applied_from_the_tip(tmp_path: Path) -> None:
    for i in range(5):
        _record(tmp_path, _URI, f"v{i}".encode(), ts=i)
    assert len(artifact_log(tmp_path, _URI, hmac_key=_KEY, limit=2)) == 2
    assert len(artifact_log(tmp_path, _URI, hmac_key=_KEY, limit=0)) == 5


def test_log_json_is_canonical(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"v1", ts=100)
    payload = artifact_log_json(artifact_log(tmp_path, _URI, hmac_key=_KEY), uri=_URI)
    assert payload == json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_log_for_an_unknown_key_is_empty(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    assert artifact_log(tmp_path, "pkg://pypi/nothing/0.0.1", hmac_key=_KEY) == ()


def test_list_reports_every_key_with_its_production_count(tmp_path: Path) -> None:
    _record(tmp_path, _URI, b"v1", ts=100)
    _record(tmp_path, _URI, b"v2", ts=200)
    _record(tmp_path, "docs/report.md", b"doc", ts=300, run_id="run-2")
    assert list_artifact_keys(tmp_path) == {_URI: 2, "docs/report.md": 1}


def test_list_on_a_project_with_no_lineage_is_empty(tmp_path: Path) -> None:
    assert list_artifact_keys(tmp_path) == {}


# ---------------------------------------------------------------------------
# Path safety carries over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "/etc/passwd",
        "a/../../etc/passwd",
        "ftp://evil.test/payload",
        "PKG://PyPI/x/1.0",
    ],
)
def test_an_unwritable_key_simply_has_no_history(tmp_path: Path, key: str) -> None:
    """Looking one up must not crash and must not resolve to anything."""
    _record(tmp_path, _URI, b"wheel", ts=100)
    assert artifact_log(tmp_path, key, hmac_key=_KEY) == ()
    assert json.loads(artifact_health_json(tmp_path, key, hmac_key=_KEY, at=500))["verdict"] == RED
