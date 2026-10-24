## Evidence-gated ladder patches for compaction retries

Compaction retries consult the configured escalation ladder, resolve failure
evidence from the verified audit chain and spend from the cost ledger, and
refuse an unpriced hop when a budget is set. Model identifiers pass through
unchanged. Task-scoped agents cannot PATCH the adapter or ladder position;
operators and the unscoped run-root identity retain that authority.

Only the compaction retry path is wired. Failure-evidence producers and the
tick-loop retry path remain follow-ups, so a configured ladder currently
refuses hops without a produced evidence event. Partial implementation of
#4855.
