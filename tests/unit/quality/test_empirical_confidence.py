"""Unit tests for the empirical-confidence ledger.

Covers:
    * record + read round trip
    * sample-size gate (returns ``None`` below threshold)
    * persistence across :class:`ConfidenceQuery` instances
    * router integration prefers empirical outcomes over the heuristic tier
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.quality.empirical_confidence import (
    DEFAULT_MIN_SAMPLES,
    Confidence,
    ConfidenceQuery,
    default_db_path,
)


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated DB path; clears env var that could leak between tests."""
    target = tmp_path / "empirical.db"
    monkeypatch.delenv("BERNSTEIN_CONFIDENCE_MIN_SAMPLES", raising=False)
    monkeypatch.setenv("BERNSTEIN_CONFIDENCE_DB", str(target))
    return target


# ---------------------------------------------------------------------------
# Basic record / confidence round trip
# ---------------------------------------------------------------------------


def test_record_then_query_returns_mean_above_threshold(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path, min_samples=3)
    for value in (1, 1, 0, 1, 0):
        query.record("router", "decision-a", value)

    result = query.get("router", "decision-a")

    assert result.samples == 5
    assert result.insufficient_data is False
    assert result.value is not None
    assert pytest.approx(result.value, abs=1e-9) == 3 / 5


def test_record_accepts_boolean(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path, min_samples=1)
    query.record("router", "k", True)
    query.record("router", "k", False)

    result = query.get("router", "k")

    assert result.samples == 2
    assert result.value == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Sample-size gate
# ---------------------------------------------------------------------------


def test_below_threshold_returns_insufficient_data(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path, min_samples=5)
    for _ in range(4):
        query.record("router", "decision-b", 1)

    result = query.get("router", "decision-b")

    assert result.samples == 4
    assert result.insufficient_data is True
    assert result.value is None


def test_threshold_default_is_five(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path)
    assert query.min_samples == DEFAULT_MIN_SAMPLES


def test_env_override_for_threshold(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_CONFIDENCE_MIN_SAMPLES", "2")
    query = ConfidenceQuery(db_path=db_path)
    assert query.min_samples == 2


def test_get_or_default_falls_back_to_prior(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path, min_samples=5)
    query.record("router", "rare-key", 1)

    assert query.get_or_default("router", "rare-key", default=0.42) == 0.42


def test_missing_key_returns_zero_samples(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path, min_samples=5)
    result = query.get("router", "unknown")

    assert result == Confidence(value=None, samples=0, insufficient_data=True, min_samples=5)


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


def test_persistence_across_instances(db_path: Path) -> None:
    writer = ConfidenceQuery(db_path=db_path, min_samples=3)
    for _ in range(3):
        writer.record("router", "persisted", 1)

    reader = ConfidenceQuery(db_path=db_path, min_samples=3)
    result = reader.get("router", "persisted")

    assert result.samples == 3
    assert result.value == pytest.approx(1.0)


def test_evidence_uri_stored(db_path: Path) -> None:
    query = ConfidenceQuery(db_path=db_path, min_samples=1)
    query.record("router", "trace", 1, evidence_uri="run://abc")

    # Cross-check by reading the raw table; the public API does not yet
    # expose evidence_uri, but the row must be present for replay tooling.
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT evidence_uri FROM agent_outcomes WHERE decision_key = ?",
            ("trace",),
        ).fetchone()
    assert row == ("run://abc",)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   "])
def test_rejects_empty_agent_type(db_path: Path, bad: str) -> None:
    query = ConfidenceQuery(db_path=db_path)
    with pytest.raises(ValueError):
        query.record(bad, "k", 1)
    with pytest.raises(ValueError):
        query.get(bad, "k")


@pytest.mark.parametrize("bad", ["", "   "])
def test_rejects_empty_decision_key(db_path: Path, bad: str) -> None:
    query = ConfidenceQuery(db_path=db_path)
    with pytest.raises(ValueError):
        query.record("router", bad, 1)


# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------


def test_default_db_path_uses_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BERNSTEIN_CONFIDENCE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    resolved = default_db_path()

    assert resolved == tmp_path / "bernstein" / "empirical-confidence.db"


# ---------------------------------------------------------------------------
# Router integration: empirical confidence beats the heuristic tier
# ---------------------------------------------------------------------------


class _StubTask:
    """Light Task stand-in matching the attributes model_recommender reads."""

    def __init__(self, role: str = "code", complexity: str = "medium", scope: str = "medium") -> None:
        self.id = "task-1"
        self.role = role
        self.model = "opus"
        self.complexity = complexity
        self.scope = scope


def _make_task(role: str = "code", complexity: str = "medium", scope: str = "medium") -> _StubTask:
    return _StubTask(role=role, complexity=complexity, scope=scope)


def test_router_prefers_empirical_confidence(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empirical samples should override the heuristic 0.8 / 0.6 tier values."""
    from bernstein.core.routing import model_recommender

    # Seed empirical outcomes for the haiku model under role 'code'.
    seeder = ConfidenceQuery(db_path=db_path, min_samples=5)
    for _ in range(5):
        seeder.record("model_recommender", "role:code|model:haiku", 1, evidence_uri="seed")

    # Stub out the cost module so the test does not need real pricing tables.
    fake_cost = MagicMock()
    fake_cost.MIN_OBSERVATIONS = 5
    fake_cost.EpsilonGreedyBandit.load.return_value = MagicMock(get_arm=MagicMock(return_value=None))
    fake_cost._model_cost.side_effect = lambda model: 0.001 if model == "haiku" else 0.01
    fake_cost.get_cascade_model.return_value = "opus"

    with patch.dict("sys.modules", {"bernstein.core.cost": fake_cost}):
        report = model_recommender.recommend_models(_make_task(complexity="low"), metrics_dir=None)

    haiku = next(r for r in report.recommendations if r.model == "haiku")
    assert haiku.confidence == pytest.approx(1.0)
    assert "Historical success rate" in haiku.reason


def test_router_falls_back_to_tier_without_sample(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without enough samples, recommender keeps the documented tier default."""
    from bernstein.core.routing import model_recommender

    seeder = ConfidenceQuery(db_path=db_path, min_samples=5)
    # Only 2 samples for haiku, below the gate.
    for _ in range(2):
        seeder.record("model_recommender", "role:code|model:haiku", 1)

    fake_cost = MagicMock()
    fake_cost.MIN_OBSERVATIONS = 5
    fake_cost.EpsilonGreedyBandit.load.return_value = MagicMock(get_arm=MagicMock(return_value=None))
    fake_cost._model_cost.side_effect = lambda model: 0.001 if model == "haiku" else 0.01
    fake_cost.get_cascade_model.return_value = "opus"

    with patch.dict("sys.modules", {"bernstein.core.cost": fake_cost}):
        report = model_recommender.recommend_models(_make_task(complexity="low"), metrics_dir=None)

    haiku = next(r for r in report.recommendations if r.model == "haiku")
    # Falls back to the heuristic tier confidence, not 1.0.
    assert haiku.confidence != pytest.approx(1.0)
    assert haiku.confidence in (0.4, 0.6, 0.8)


# ---------------------------------------------------------------------------
# Schema migration from the pre-``sampled_at`` layout
# ---------------------------------------------------------------------------


_LEGACY_SCHEMA = """
CREATE TABLE agent_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type    TEXT NOT NULL,
    decision_key  TEXT NOT NULL,
    outcome       INTEGER NOT NULL CHECK (outcome IN (0, 1)),
    evidence_uri  TEXT,
    recorded_at   REAL NOT NULL
);
CREATE INDEX idx_agent_outcomes_lookup
    ON agent_outcomes (agent_type, decision_key);
"""


def _make_legacy_db(path: Path, rows: list[tuple[str, str, int, str | None, float]]) -> None:
    """Write a database using the pre-``sampled_at`` column layout."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO agent_outcomes "
            "(agent_type, decision_key, outcome, evidence_uri, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(agent_outcomes)")]
    finally:
        conn.close()


def test_legacy_database_is_migrated_and_stays_usable(db_path: Path) -> None:
    """An install carrying the old ``recorded_at`` column keeps working."""
    _make_legacy_db(
        db_path,
        [
            ("router", "decision-a", 1, "run://1", 100.0),
            ("router", "decision-a", 0, None, 200.0),
        ],
    )

    query = ConfidenceQuery(db_path=db_path, min_samples=1)

    assert "sampled_at" in _columns(db_path)
    assert "recorded_at" not in _columns(db_path)

    # Pre-existing rows survive the rename, timestamps included.
    conn = sqlite3.connect(db_path)
    try:
        preserved = conn.execute(
            "SELECT agent_type, decision_key, outcome, evidence_uri, sampled_at FROM agent_outcomes ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert preserved == [
        ("router", "decision-a", 1, "run://1", 100.0),
        ("router", "decision-a", 0, None, 200.0),
    ]

    # And the ledger accepts new writes on the migrated table.
    query.record("router", "decision-a", 1, evidence_uri="run://3")

    result = query.get("router", "decision-a")
    assert result.samples == 3
    assert result.value == pytest.approx(2 / 3)


def test_migration_is_idempotent_across_reopens(db_path: Path) -> None:
    """Re-opening a migrated database neither re-renames nor loses rows."""
    _make_legacy_db(db_path, [("router", "k", 1, None, 100.0)])

    ConfidenceQuery(db_path=db_path, min_samples=1).record("router", "k", 0)
    reopened = ConfidenceQuery(db_path=db_path, min_samples=1)
    reopened.record("router", "k", 1)

    assert _columns(db_path).count("sampled_at") == 1
    result = reopened.get("router", "k")
    assert result.samples == 3
    assert result.value == pytest.approx(2 / 3)


def test_fresh_database_is_unaffected_by_the_migration(db_path: Path) -> None:
    """A database created from scratch still gets the current schema."""
    query = ConfidenceQuery(db_path=db_path, min_samples=1)
    query.record("router", "k", 1)

    columns = _columns(db_path)
    assert "sampled_at" in columns
    assert "recorded_at" not in columns
    assert query.get("router", "k").samples == 1
