# Empirical confidence from outcome history

Routing and review surfaces previously relied on a model's self-reported
confidence or a single recent run's outcome. Self-rated confidence is
uncorrelated with actual correctness, and a single run is too noisy to gate
decisions on. This module records explicit outcomes and exposes a
sample-size-gated query.

## Module

`src/bernstein/core/quality/empirical_confidence.py`

Public API:

- `record_outcome(agent_type, decision_key, outcome, *, evidence_uri=None, sampled_at=None)`
- `confidence(agent_type, decision_key) -> Confidence`
- `ConfidenceQuery(db_path=..., min_samples=...)` for dependency injection

Return type:

```python
@dataclass(frozen=True)
class Confidence:
    value: float | None  # mean outcome in [0,1], or None
    samples: int  # total recorded rows
    insufficient_data: bool  # True when samples < min_samples
    min_samples: int  # threshold used for this query
```

## Storage

Single SQLite table, append-only:

```
agent_outcomes(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type   TEXT    NOT NULL,
    decision_key TEXT    NOT NULL,
    outcome      INTEGER NOT NULL CHECK (outcome IN (0, 1)),
    sampled_at   REAL    NOT NULL,   -- POSIX seconds, UTC
    evidence_uri TEXT
);
CREATE INDEX idx_agent_outcomes_lookup ON agent_outcomes (agent_type, decision_key);
```

Default path: `${XDG_DATA_HOME:-~/.local/share}/bernstein/empirical-confidence.db`.

## Sample-size gate

The default minimum sample count is `5`. Below this threshold,
`confidence(...)` returns `value=None` and `insufficient_data=True`. The
caller is expected to fall back to a documented uniform prior (the module
exposes `DEFAULT_PRIOR = 0.5`) or another signal of its choice.

Override via:

- `BERNSTEIN_CONFIDENCE_MIN_SAMPLES` environment variable
- `ConfidenceQuery(min_samples=...)` constructor argument

## Routing integration

`bernstein.core.routing.model_recommender.recommend_models` queries the
empirical ledger first under the decision key
`role:<task.role>|model:<model_key>`. The previous epsilon-greedy bandit
data still fills in for cells that have not yet accumulated enough samples
in the ledger, and the capability-tier heuristic is the final fallback.

The ordering is:

1. Empirical confidence with `samples >= min_samples`.
2. Bandit arm success rate with `observations >= MIN_OBSERVATIONS`.
3. Capability-tier heuristic (0.8 / 0.6 / 0.4).

## Why decoupled from the run log

Outcome population may lag the run that produced it (a review verdict can
arrive minutes or hours later, or be replayed). Keeping a separate
append-only table lets the run-log semantics stay simple and lets backfill
or replay add rows without touching run history.

## Configuration summary

| Variable                             | Default                                                | Effect                          |
|--------------------------------------|--------------------------------------------------------|---------------------------------|
| `BERNSTEIN_CONFIDENCE_DB`            | `~/.local/share/bernstein/empirical-confidence.db`     | Override SQLite file location   |
| `BERNSTEIN_CONFIDENCE_MIN_SAMPLES`   | `5`                                                    | Sample-size gate threshold      |
| `XDG_DATA_HOME`                      | `~/.local/share`                                       | Base for the default DB path    |
