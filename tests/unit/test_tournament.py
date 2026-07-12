"""Tournament runs: deterministic parallel-attempt selection (issue #2353).

Acceptance criteria under test:

* AC1 -- same evaluator outputs always produce a byte-identical selection
  decision on replay (hash-asserted).
* AC2 -- the selection receipt is verifiable offline and names all attempt
  hashes and scores.
* AC3 -- N attempts appear in lineage as siblings with exactly one chosen edge.
* AC4 -- fan-out aborts with a clear error when projected spend exceeds the
  task budget ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.tournament.evaluators import AttemptOutcome, EvaluatorOutput
from bernstein.core.tournament.receipt import (
    CHOSEN_RELATION,
    SIBLING_RELATION,
    emit_tournament_receipt,
    load_or_create_tournament_identity,
    read_tournament_receipt,
    verify_all_tournament_receipts,
    verify_tournament_receipt,
)
from bernstein.core.tournament.runner import (
    TournamentBudgetExceeded,
    check_fanout_budget,
    resolve_fanout_cap,
)
from bernstein.core.tournament.scorer import (
    score_attempt,
    select_winner,
    selection_projection_bytes,
)
from bernstein.core.tournament.spec import (
    EvaluatorSpec,
    TournamentSpec,
    TournamentSpecError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec() -> TournamentSpec:
    return TournamentSpec(
        attempts=3,
        evaluators=(
            EvaluatorSpec(name="tests", weight=0.6),
            EvaluatorSpec(name="lint", weight=0.3),
            EvaluatorSpec(name="runtime", weight=0.1, higher_is_better=False),
        ),
    )


def _outcomes() -> list[AttemptOutcome]:
    # Three attempts, each with a content-addressed attempt hash derived from
    # distinct output bytes, plus a fixed set of evaluator outputs.
    return [
        AttemptOutcome.from_output_bytes(
            b"attempt-A",
            outputs=(
                EvaluatorOutput(name="tests", value=1.0),
                EvaluatorOutput(name="lint", value=1.0),
                EvaluatorOutput(name="runtime", value=0.4),
            ),
        ),
        AttemptOutcome.from_output_bytes(
            b"attempt-B",
            outputs=(
                EvaluatorOutput(name="tests", value=1.0),
                EvaluatorOutput(name="lint", value=0.5),
                EvaluatorOutput(name="runtime", value=0.1),
            ),
        ),
        AttemptOutcome.from_output_bytes(
            b"attempt-C",
            outputs=(
                EvaluatorOutput(name="tests", value=0.0),
                EvaluatorOutput(name="lint", value=1.0),
                EvaluatorOutput(name="runtime", value=0.9),
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Spec schema + validation
# ---------------------------------------------------------------------------


def test_spec_from_task_dict_roundtrip() -> None:
    raw = {
        "attempts": 3,
        "evaluators": [
            {"name": "tests", "weight": 0.6},
            {"name": "lint", "weight": 0.3},
            {"name": "runtime", "weight": 0.1, "higher_is_better": False},
        ],
        "tie_break": "attempt_hash",
    }
    spec = TournamentSpec.from_task_dict(raw)
    assert spec.attempts == 3
    assert [e.name for e in spec.evaluators] == ["tests", "lint", "runtime"]
    assert spec.evaluators[2].higher_is_better is False


def test_spec_rejects_no_evaluators() -> None:
    with pytest.raises(TournamentSpecError):
        TournamentSpec(attempts=2, evaluators=())


def test_spec_rejects_negative_weight() -> None:
    with pytest.raises(TournamentSpecError):
        TournamentSpec(attempts=2, evaluators=(EvaluatorSpec(name="x", weight=-1.0),))


def test_spec_hash_is_stable() -> None:
    assert _spec().spec_hash() == _spec().spec_hash()


# ---------------------------------------------------------------------------
# AC1 -- deterministic, byte-identical selection on replay
# ---------------------------------------------------------------------------


def test_selection_projection_is_byte_identical_on_replay() -> None:
    spec = _spec()
    outcomes = _outcomes()
    first = selection_projection_bytes(outcomes, spec)
    # Replay: rebuild the outcomes from scratch and reorder the input list.
    replay = selection_projection_bytes(list(reversed(_outcomes())), spec)
    assert first == replay


def test_winner_is_deterministic_and_scored() -> None:
    spec = _spec()
    outcomes = _outcomes()
    selection = select_winner(outcomes, spec)
    # Attempt A: 0.6*1 + 0.3*1 + 0.1*(1-0.4) = 0.96
    # Attempt B: 0.6*1 + 0.3*0.5 + 0.1*(1-0.1) = 0.84
    # Attempt C: 0.6*0 + 0.3*1 + 0.1*(1-0.9) = 0.31
    a_hash = outcomes[0].attempt_hash
    assert selection.winner_hash == a_hash
    assert selection.ranked[0].attempt_hash == a_hash
    assert score_attempt(outcomes[0], spec) == pytest.approx(0.96)


def test_tie_break_is_attempt_hash_order() -> None:
    spec = TournamentSpec(attempts=2, evaluators=(EvaluatorSpec(name="tests", weight=1.0),))
    tied = [
        AttemptOutcome.from_output_bytes(b"zzz", outputs=(EvaluatorOutput(name="tests", value=1.0),)),
        AttemptOutcome.from_output_bytes(b"aaa", outputs=(EvaluatorOutput(name="tests", value=1.0),)),
    ]
    winner = select_winner(tied, spec).winner_hash
    # Both score 1.0; the lexicographically smaller attempt hash wins.
    smallest = min(o.attempt_hash for o in tied)
    assert winner == smallest


# ---------------------------------------------------------------------------
# AC2 + AC3 -- receipt: offline verify, names attempts+scores, sibling edges
# ---------------------------------------------------------------------------


def _emit(tmp_path: Path) -> tuple[Path, bytes]:
    workdir = tmp_path
    lineage_root = workdir / ".sdd" / "lineage"
    hmac_key = load_or_create_audit_key(workdir / ".sdd" / "audit.key")
    priv, pub = load_or_create_tournament_identity(workdir / ".sdd" / "tournaments")
    emit_tournament_receipt(
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id="T-1",
        spec=_spec(),
        outcomes=_outcomes(),
        timestamp=1234567890,
    )
    return workdir, hmac_key


def test_receipt_names_all_attempts_and_scores(tmp_path: Path) -> None:
    workdir, _ = _emit(tmp_path)
    receipt = read_tournament_receipt(workdir, "T-1")
    assert receipt is not None
    recorded = {a.attempt_hash for a in receipt.attempts}
    assert recorded == {o.attempt_hash for o in _outcomes()}
    assert len(receipt.scores) == 3
    # Every attempt hash has a recorded score.
    assert {s.attempt_hash for s in receipt.scores} == recorded


def test_receipt_verifies_offline(tmp_path: Path) -> None:
    workdir, hmac_key = _emit(tmp_path)
    result = verify_tournament_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        task_id="T-1",
    )
    assert result.ok, result.reason


def test_receipt_has_exactly_one_chosen_edge(tmp_path: Path) -> None:
    workdir, _ = _emit(tmp_path)
    receipt = read_tournament_receipt(workdir, "T-1")
    assert receipt is not None
    chosen = [e for e in receipt.edges if e.relation == CHOSEN_RELATION]
    siblings = [e for e in receipt.edges if e.relation == SIBLING_RELATION]
    assert len(chosen) == 1
    assert chosen[0].attempt_hash == receipt.winner_hash
    # Every attempt is represented exactly once across sibling+chosen edges.
    assert len(chosen) + len(siblings) == len(_outcomes())
    assert {e.attempt_hash for e in receipt.edges} == {o.attempt_hash for o in _outcomes()}


def test_tampered_score_fails_verify(tmp_path: Path) -> None:
    workdir, hmac_key = _emit(tmp_path)
    # Flip a recorded score so it no longer matches the deterministic replay.
    from bernstein.core.tournament.receipt import receipt_path

    path = receipt_path(workdir, "T-1")
    row = json.loads(path.read_text())
    row["scores"][0]["score"] = 0.123
    path.write_text(json.dumps(row, separators=(",", ":"), sort_keys=True))
    result = verify_tournament_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        task_id="T-1",
    )
    assert not result.ok


def test_tampered_winner_fails_verify(tmp_path: Path) -> None:
    workdir, hmac_key = _emit(tmp_path)
    from bernstein.core.tournament.receipt import receipt_path

    path = receipt_path(workdir, "T-1")
    row = json.loads(path.read_text())
    # Point the winner at a losing attempt without re-running selection.
    losing = _outcomes()[2].attempt_hash
    row["winner_hash"] = losing
    path.write_text(json.dumps(row, separators=(",", ":"), sort_keys=True))
    result = verify_tournament_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        task_id="T-1",
    )
    assert not result.ok


def test_verify_all_covers_receipt(tmp_path: Path) -> None:
    workdir, hmac_key = _emit(tmp_path)
    results = verify_all_tournament_receipts(workdir, hmac_key=hmac_key)
    assert len(results) == 1
    assert results[0].ok


# ---------------------------------------------------------------------------
# AC4 -- fan-out budget abort
# ---------------------------------------------------------------------------


def test_check_fanout_budget_aborts_over_cap() -> None:
    with pytest.raises(TournamentBudgetExceeded) as excinfo:
        check_fanout_budget(attempts=5, per_attempt_cost_usd=2.0, cap_usd=4.0)
    msg = str(excinfo.value)
    assert "5" in msg  # attempts
    assert "10" in msg  # projected 5 * 2.0


def test_check_fanout_budget_allows_under_cap() -> None:
    projected = check_fanout_budget(attempts=3, per_attempt_cost_usd=1.0, cap_usd=10.0)
    assert projected == pytest.approx(3.0)


def test_check_fanout_budget_no_cap_is_permissive() -> None:
    # No cap configured -> never aborts.
    projected = check_fanout_budget(attempts=100, per_attempt_cost_usd=5.0, cap_usd=None)
    assert projected == pytest.approx(500.0)


def test_resolve_fanout_cap_prefers_ticket_cap() -> None:
    cap = resolve_fanout_cap(ticket_cap=3.5, default_cap=100.0)
    assert cap == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Runner -- fan-out barrier + budget gate (AC4) + receipt emission
# ---------------------------------------------------------------------------


def test_runner_aborts_before_spawning_when_over_budget(tmp_path: Path) -> None:
    from bernstein.core.tournament.runner import TournamentRunner

    spawned: list[int] = []

    def spawner(task_id: str, n: int) -> list[str]:
        spawned.append(n)
        return [f"{task_id}-{i}" for i in range(n)]

    def evaluator(ids: list[str]) -> list[AttemptOutcome]:  # pragma: no cover - never reached
        return _outcomes()

    runner = TournamentRunner(spawner=spawner, evaluator=evaluator)
    with pytest.raises(TournamentBudgetExceeded):
        runner.run(
            task_id="T-9",
            spec=_spec(),  # attempts=3
            per_attempt_cost_usd=5.0,  # projected 15 > cap 4
            cap_usd=4.0,
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=load_or_create_audit_key(tmp_path / ".sdd" / "audit.key"),
            private_key_pem="",
            public_key_pem="",
            timestamp=1,
        )
    # Fan-out never started: the spawner was not called.
    assert spawned == []


def test_runner_emits_verifiable_receipt(tmp_path: Path) -> None:
    from bernstein.core.tournament.runner import TournamentRunner

    priv, pub = load_or_create_tournament_identity(tmp_path / ".sdd" / "tournaments")
    hmac_key = load_or_create_audit_key(tmp_path / ".sdd" / "audit.key")
    reclaimed: list[str] = []

    runner = TournamentRunner(
        spawner=lambda task_id, n: [f"{task_id}-{i}" for i in range(n)],
        evaluator=lambda ids: _outcomes(),
        reclaimer=lambda outcome: reclaimed.append(outcome.attempt_hash),
    )
    outcome = runner.run(
        task_id="T-run",
        spec=_spec(),
        per_attempt_cost_usd=0.5,
        cap_usd=100.0,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        timestamp=42,
    )
    result = verify_tournament_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        task_id="T-run",
    )
    assert result.ok, result.reason
    # Losers reclaimed, winner kept.
    assert outcome.winner_hash not in reclaimed
    assert len(reclaimed) == 2


# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------


def test_audit_event_records_selection(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import (
        EVENT_TOURNAMENT_SELECTION,
        AuditChainStore,
        record_tournament_selection,
    )

    chain = AuditChainStore(tmp_path / "audit")
    outcomes = _outcomes()
    event = record_tournament_selection(
        chain=chain,
        task_id="T-1",
        receipt_hash="sha256:deadbeef",
        winner_hash=outcomes[0].attempt_hash,
        attempt_hashes=[o.attempt_hash for o in outcomes],
        evaluator_names=["tests", "lint", "runtime"],
    )
    assert event.event_type == EVENT_TOURNAMENT_SELECTION
    assert event.details["winner_hash"] == outcomes[0].attempt_hash
    assert event.details["attempt_count"] == 3


# ---------------------------------------------------------------------------
# Cache-window fan-out (#2354, AC4)
# ---------------------------------------------------------------------------


def test_cache_window_fanout_warms_once_before_siblings_on_capable_adapter(tmp_path: Path) -> None:
    """A capable + enabled adapter primes the shared prefix once, then M siblings hit it."""
    from bernstein.core.tournament.runner import CacheWindowFanout, TournamentRunner

    priv, pub = load_or_create_tournament_identity(tmp_path / ".sdd" / "tournaments")
    hmac_key = load_or_create_audit_key(tmp_path / ".sdd" / "audit.key")
    order: list[str] = []

    def spawner(task_id: str, n: int) -> list[str]:
        order.append(f"spawn:{n}")
        return [f"{task_id}-{i}" for i in range(n)]

    def warmup() -> None:
        order.append("warmup")

    runner = TournamentRunner(
        spawner=spawner,
        evaluator=lambda ids: _outcomes(),
        cache_window=CacheWindowFanout(adapter="claude", prefix="shared-system-prompt", warmup=warmup, enabled=True),
    )
    outcome = runner.run(
        task_id="T-cache",
        spec=_spec(),  # attempts=3
        per_attempt_cost_usd=0.5,
        cap_usd=100.0,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        timestamp=7,
    )
    # One warm-up strictly precedes the three sibling spawns.
    assert order == ["warmup", "spawn:1", "spawn:1", "spawn:1"]
    assert outcome.cache_fanout is not None
    assert outcome.cache_fanout.warmup_calls_made == 1
    assert outcome.cache_fanout.worker_calls_made == 3
    assert outcome.cache_fanout.cache_hits == 3


def test_cache_window_disabled_issues_no_warmup(tmp_path: Path) -> None:
    """With the window disabled, the fan-out issues no warm-up and expects no hits."""
    from bernstein.core.tournament.runner import CacheWindowFanout, TournamentRunner

    priv, pub = load_or_create_tournament_identity(tmp_path / ".sdd" / "tournaments")
    hmac_key = load_or_create_audit_key(tmp_path / ".sdd" / "audit.key")
    warmups: list[int] = []

    runner = TournamentRunner(
        spawner=lambda task_id, n: [f"{task_id}-{i}" for i in range(n)],
        evaluator=lambda ids: _outcomes(),
        cache_window=CacheWindowFanout(
            adapter="claude", prefix="shared", warmup=lambda: warmups.append(1), enabled=False
        ),
    )
    outcome = runner.run(
        task_id="T-off",
        spec=_spec(),
        per_attempt_cost_usd=0.5,
        cap_usd=100.0,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        timestamp=8,
    )
    assert warmups == []
    assert outcome.cache_fanout is not None
    assert outcome.cache_fanout.warmup_calls_made == 0
    assert outcome.cache_fanout.cache_hits == 0


def test_cache_window_incapable_adapter_issues_no_warmup(tmp_path: Path) -> None:
    """An adapter with no cache window issues no warm-up even when enabled."""
    from bernstein.core.tournament.runner import CacheWindowFanout, TournamentRunner

    priv, pub = load_or_create_tournament_identity(tmp_path / ".sdd" / "tournaments")
    hmac_key = load_or_create_audit_key(tmp_path / ".sdd" / "audit.key")
    warmups: list[int] = []

    runner = TournamentRunner(
        spawner=lambda task_id, n: [f"{task_id}-{i}" for i in range(n)],
        evaluator=lambda ids: _outcomes(),
        cache_window=CacheWindowFanout(adapter="mock", prefix="shared", warmup=lambda: warmups.append(1), enabled=True),
    )
    outcome = runner.run(
        task_id="T-incap",
        spec=_spec(),
        per_attempt_cost_usd=0.5,
        cap_usd=100.0,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        timestamp=9,
    )
    assert warmups == []  # mock is not a cache-window adapter
    assert outcome.cache_fanout is not None
    assert outcome.cache_fanout.warmup_calls_made == 0
    assert outcome.cache_fanout.cache_hits == 0


def test_runner_without_cache_window_spawns_in_one_call(tmp_path: Path) -> None:
    """The default (no cache window) fans out siblings in one spawner call, unchanged."""
    from bernstein.core.tournament.runner import TournamentRunner

    priv, pub = load_or_create_tournament_identity(tmp_path / ".sdd" / "tournaments")
    hmac_key = load_or_create_audit_key(tmp_path / ".sdd" / "audit.key")
    calls: list[int] = []

    runner = TournamentRunner(
        spawner=lambda task_id, n: (calls.append(n), [f"{task_id}-{i}" for i in range(n)])[1],
        evaluator=lambda ids: _outcomes(),
    )
    outcome = runner.run(
        task_id="T-plain",
        spec=_spec(),
        per_attempt_cost_usd=0.5,
        cap_usd=100.0,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        timestamp=10,
    )
    assert calls == [3]  # one call, n=attempts
    assert outcome.cache_fanout is None
