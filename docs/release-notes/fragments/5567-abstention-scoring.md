## A declined task no longer scores like a wrong one

A benchmark run that declined a task it could not verify was recorded as
`failed`, indistinguishable from one that submitted a confidently wrong patch.
Both sat in the denominator of `resolve_rate = resolved / attempted` and
neither in the numerator, so the score rewarded guessing: a guess can only
raise the resolve rate, and an abstention can only lower it.

An instance may now end `abstained`, carrying an `abstention_reason` — separate
from `error_message`, which says the harness broke rather than that the run
declined. An abstention is excluded from `attempted`, and because it scores
above a wrong answer, claiming one costs a stated reason.

Summaries report three rates instead of one: the resolve rate (abstentions out
of the denominator), the abstain rate, and the confident-error rate,
`wrong / (wrong + resolved)`. Harness errors are excluded from both halves of
that last one. Read together they separate a run that answers rarely and well
from one that answers everything and is often wrong — a distinction the resolve
rate alone cannot make, which is what made guessing free.

Existing bundles are unaffected: with no abstentions, `attempted` is
`total - skipped` exactly as before and published resolve rates do not move.

This is the metrics half of #5567. The per-suite lambda, `Score.value` and the
`bench compare` ranking wait on the suite protocol (#5444).
