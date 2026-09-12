## Benchmarks reward declared abstentions above wrong answers and report confident-error rate

`InstanceStatus` and `TaskResult` now support declared task abstentions (`status="abstained"`).
Evaluation suites score resolved instances as $+1.0$, abstained instances as $0.0$, and wrong
attempts as $-\lambda$ (where penalty parameter $\lambda$ defaults to $1.0$ with a per-suite
override).

`ScenarioSummary` and `SubmissionBundle` report three decoupled rates:
- **`resolve_rate`**: $\frac{\text{resolved}}{\text{attempted}}$ (abstentions excluded from denominator, preserving backwards compatibility for legacy bundles without abstentions)
- **`abstain_rate`**: $\frac{\text{abstained}}{\text{total} - \text{skipped}}$
- **`confident_error_rate`**: $\frac{\text{wrong}}{\text{wrong} + \text{resolved}}$

`bernstein bench compare` ranks bundles by expected value under $\lambda$ (`--penalty` / `--lambda`)
and displays all three rates. Tasks record declared confidence against observed resolution outcome,
and bundles serialize the resulting Brier score (#5567).
