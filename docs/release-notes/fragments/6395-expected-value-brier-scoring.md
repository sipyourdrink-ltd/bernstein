## Expected-value and Brier-calibration scoring in scenario summaries

SWE-bench and scenario summaries now compute expected value (`expected_value`) under a risk penalty parameter (`lambda_penalty`, default 0.5, settable per call on `aggregate()`) and a Brier calibration score (`brier_score`) measuring how well a run's *declared* confidence tracked its actual outcome (#5923).

The two metrics count different things, deliberately:

| outcome | in EV's denominator | in Brier's mean |
| --- | --- | --- |
| resolved / failed | yes | yes, if a confidence was declared |
| harness error | yes — the run attempted it | no — it made no prediction |
| abstained | no — a declared non-answer is not an attempt | no |
| skipped | no | no |

λ weights *wrongness*, so a harness error incurs no penalty while still counting as an attempt. A result that declared no confidence leaves the Brier mean entirely rather than contributing an assumed one: calibration measures what a run said, and a number it never said is not evidence about it. `confidence` is therefore `None` when undeclared, not a default, and is rejected outside `[0, 1]`.

Brier differs from `confident_error_rate` (#5905): that measures how often a run was confidently wrong, this measures how far its stated probabilities sat from reality across every prediction it made.
