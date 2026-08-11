# Issue -> PR pipeline

## TL;DR

| Stage | Side effect |
|-------|-------------|
| `plan` | Posts a sticky markdown plan as an issue comment. |
| `approval` | Polls the sticky comment for a thumbs-up reaction or the configured approval keyword. |
| `pr_open` | Applies the diff, pushes a branch, opens a draft PR, links back to the plan comment. |
| `pr_revise` | Reads inline review comments newer than the last revision marker, dispatches an agent with the comments as context, pushes a follow-up commit. |

Each stage writes a state marker (HTML comment inside the sticky plan
comment) so re-running a stage that already completed is a no-op.  The
pipeline never auto-merges the resulting PR; merge gating stays with the
operator.

## Configuration

Add an `orchestration.issue_to_pr` block to `bernstein.yaml`:

```yaml
orchestration:
  issue_to_pr:
    repos:
      - {owner: acme, name: web}
      - {owner: acme, name: api}
    triggers:
      label_required: ai-welcome
      author_allow_list: [alice, bob]
    stages:
      plan_comment_required_approval: true
      draft_pr_default: true
      approval_keyword: "[approved]"
      revise_quiet_window_s: 60
```

Field reference:

| Key | Type | Meaning |
|-----|------|---------|
| `repos` | list of `{owner, name}` | Repositories the pipeline operates on. Empty means "any repo". |
| `triggers.label_required` | string | Issue must carry this label to qualify. `null` disables the gate. |
| `triggers.author_allow_list` | list of GitHub logins | Only issues opened by these users are processed. Empty list disables the gate. |
| `stages.plan_comment_required_approval` | bool | When true (default), the pipeline pauses after posting the plan and waits for a thumbs-up or the approval keyword. |
| `stages.draft_pr_default` | bool | When true (default), PRs are opened in draft state. |
| `stages.approval_keyword` | string | Phrase that grants approval when posted by a user on the allow-list. Defaults to `[approved]`. |
| `stages.revise_quiet_window_s` | float | Minimum age (seconds) of an inline review comment before the revise stage picks it up. |

## Setup steps

1. Configure the GitHub App per `docs/operations/github-app.md` so the
   pipeline has installation tokens for the `repos` it touches.
2. Add the `orchestration.issue_to_pr` block to `bernstein.yaml`.
3. Wire the three injection points when constructing the pipeline:
   - `plan_generator` -- callable that turns an `IssueContext` into a
     `PlanProposal`.  Typical wiring: dispatch a Bernstein task with the
     issue body as context and parse the agent output as markdown.
   - `diff_generator` -- callable that produces the initial diff once
     approval lands.  Typical wiring: spawn a Claude/Codex/Aider session
     in a fresh worktree and capture `git diff` as the patch.
   - `revise_generator` -- callable that, given the new inline review
     comments, produces a follow-up diff.
   - `apply_diff` -- callable that applies the patch to a working tree,
     pushes the branch, and returns the head SHA.
4. Drive the pipeline from the autofix daemon, a cron job, or a manual
   call to `pipeline.tick(repo, issue_number)`.

## CLI

```
bernstein issue-to-pr trace --repo acme/web 42
```

Reads the sticky-comment markers and prints one fact per line:

```
repo:           acme/web
issue:          #42
plan_posted:    True
approved:       True
pr_number:      4242
last_revise_at: 2026-05-19T12:00:00Z
```

The command is read-only; it never advances state.

## State markers

The pipeline stores its progress as HTML comments inside the sticky
plan comment so the state is fully recoverable from GitHub alone (no
sidecar database).

| Marker | Meaning |
|--------|---------|
| `<!-- bernstein:issue-to-pr:plan -->` | Identifies the sticky plan comment in the issue thread. |
| `<!-- stage:plan:done -->` | Plan-comment stage finished. |
| `<!-- stage:approval:granted -->` | Approval recorded. |
| `<!-- stage:pr-open:done pr=<N> -->` | Draft PR `#<N>` opened. |
| `<!-- stage:pr-revise:last=<iso> sha=<sha> -->` | Last revise round; oldest unprocessed review comment is one with `created_at > <iso>`. |

Re-running any stage that already wrote its marker is a no-op.

## Out of scope

- GitLab MR variant (separate adapter pickup once the GitLab tracker
  adapter lands).
- Issue triage / auto-labelling.
- Auto-merge of the resulting PR; merge stays operator-gated.
