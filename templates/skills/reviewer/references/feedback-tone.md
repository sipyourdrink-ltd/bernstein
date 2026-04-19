# Feedback tone and severity

## Labels
- **blocking** — merge cannot proceed without addressing this.
- **suggestion** — would improve the change; author's call.
- **nit** — preference or micro-optimisation; never blocks.
- **question** — asking for context; not a request to change code.

## Style
- Lead with the problem, not the fix. "This unbounded loop can OOM on
  large inputs" beats "Change to `for i, item in enumerate(batch[:100]):`".
- Offer an alternative when you reject an approach.
- Use "we" when the convention is shared; "I'd suggest" when it's personal taste.
- No sarcasm, no snark — even when the bug is avoidable.
- Approve when blockers are green; do not hold open PRs over nits.

## When to re-request review
- Author pushed a fix for a blocker → reviewer re-runs the rubric.
- Reviewer made a mistake → reviewer retracts publicly.
- Scope grew beyond the original description → reviewer can request a split.
