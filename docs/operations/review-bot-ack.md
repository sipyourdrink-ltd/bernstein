# Review-bot acknowledgement protocol

This page documents the pre-merge gate and post-merge sweeper that
ensure CodeRabbit and Sourcery findings on PRs are processed.

## Why

Automated review tools regularly flag legitimate correctness and
security issues that hand-reviews miss. Treating their output as
advisory means real defects ship to `main`. This protocol makes the
findings part of the merge gate.

## The gate

`.github/workflows/review-bot-ack.yml` runs on every PR event and on
every review submission. It calls `scripts/review_bot_ack.py`, which:

1. Fetches inline review-comment threads (`pulls/<n>/comments`) and
   top-level issue comments (`issues/<n>/comments`) authored by the
   `coderabbitai[bot]` and `sourcery-ai[bot]` accounts.
2. Classifies each comment into one of two buckets based on the
   severity tag the bot embeds in the body
   (`**Potential issue**`, `**issue:**`, `**security:**`,
   `**suggestion (security):**`, etc.):
   - `must-address`: bug, security, potential issue,
     refactor-with-correctness-implication.
   - `informational`: note, nit, style, refactor suggestion, testing
     suggestion.
3. Confirms every `must-address` finding is either:
   - Fixed in a commit on the PR branch whose message contains
     `bot-ack: <comment-id>` or `addresses: <comment-id>`, OR
   - Acknowledged in the PR body with
     `<!-- bot-ack: <comment-id> reason=<short-reason> -->`.
4. Upserts a sticky summary comment on the PR (marker:
   `<!-- review-bot-ack-summary: managed -->`) listing open findings.
5. Exits non-zero if any `must-address` finding is unresolved; that
   non-zero exit fails the `review-bot-ack` check and blocks merge.

### Skipping nit/style findings

Informational findings are not gated. The single line
`<!-- bot-ack: nit-batch-skipped -->` in the PR body is a
documentation hint for human reviewers; the gate does not require it.

## The sweeper

`.github/workflows/review-bot-sweep.yml` runs daily at 06:00 UTC and
on `workflow_dispatch`. It walks merged PRs from the configurable
look-back window (default 30 days) and runs the same classifier. Any
PR with unresolved `must-address` findings is reported in a manifest;
the workflow opens a consolidated follow-up PR
(`fix(review): apply deferred review-bot findings`) carrying that
manifest. The workflow authenticates with `GITHUB_TOKEN`.

## Shepherd checklist

Shepherds:

1. Watch CI to green.
2. Fetch all CodeRabbit + Sourcery comments via the two `gh api`
   endpoints listed above.
3. Classify into must-address vs informational.
4. Apply must-address fixes in a fixup commit (`bot-ack: <id>` in
   the message) or add a `bot-ack` marker to the PR body with a
   short reason.
5. Push, re-watch CI, then `gh pr merge --auto --squash`.

## Files

- `.github/workflows/review-bot-ack.yml` - pre-merge gate.
- `.github/workflows/review-bot-sweep.yml` - daily post-merge sweep.
- `scripts/review_bot_ack.py` - classifier + acknowledgement check.
- `scripts/review_bot_sweep.py` - sweep + manifest renderer.
- `tests/unit/test_review_bot_ack_workflow_yaml.py` - structural and
  classifier assertions.
