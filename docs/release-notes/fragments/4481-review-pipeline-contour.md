## Review runs to an outcome, and every pass leaves a receipt

`bernstein review --pipeline` stopped at a verdict, so the loop that turns a
verdict into an outcome on the pull request lived in whatever shell an operator
wrote around the CLI, each with its own stop condition and no record of what was
reviewed. `--fix --until-checks-green --max-passes N` now runs that loop inside
the product: wait for the check rollup to settle, review the current diff, hand
the verdict and the failing checks' log excerpts to a fix pass, repeat. A spent
budget exits non-zero with a `needs-operator` outcome rather than an approval. A
repository can declare the standard the review is held to in
`.bernstein/review-rules.md` -- defect classes to raise, plus the findings not to
raise -- and every pass emits a signed, spine-anchored receipt binding the
reviewed diff hash, that ruleset's digest, the pass index and the verdict.
`review-receipt verify --chain` walks the sequence offline and rejects a pass
whose diff or ruleset moved. With no rules file the pipeline behaves exactly as
before (#4481).
