## Governance coverage for a run

`GET /governance/coverage?run_id=<run>` reports what a run's recorded evidence can account for: the fraction of its actions whose actor is named as the subject of a governance decision, and the fraction whose actor holds a recorded `allow` verdict. Decision records and chain bookkeeping are excluded from the action count so a run cannot raise its own coverage by recording more decisions, a run with no recorded actions reports an absent ratio rather than zero or one, and the spine verify status travels with the numbers so a tampered chain cannot read as a clean one.

(#5067)
