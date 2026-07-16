# Review responder

`bernstein review-responder` is a daemon that turns inline GitHub PR
review comments into Bernstein tasks, dispatches a coding agent, and
posts back a single summary reply per review round. It is meant for
maintainers who already merge Bernstein-produced PRs and want the same
deterministic loop to handle "fix this typo on line 42" feedback
without a human re-running the orchestrator.

The CLI lives in `cli/commands/review_responder_cmd.py:46`
(`@click.group("review-responder")`). Heavy logic lives in
`core/review_responder/` (10 files). The CLI is intentionally thin -
it just glues click flags to the responder primitives and prints a
status summary (`review_responder_cmd.py`).

A sibling daemon - [`operations/autofix.md`](autofix.md) - handles the "CI failed, retry" path. The two are designed
to compose: autofix repairs the PR, review-responder closes out the
remaining review comments.

---

## What it does

The responder:

1. **Listens** to GitHub for new inline review comments via either a
   webhook listener (`WebhookListener`) or a polling fallback
   (`PollingListener`) (`core/review_responder/__init__.py:1-22`).
2. **Bundles** comments arriving inside a configurable quiet window
   into a single round (`RoundBundler`,
   `core/review_responder/bundling.py`). Default quiet window: 90
   seconds.
3. **Decides** per comment whether to address, dismiss as stale, or
   dismiss as a discussion question (`CommentDecision` in
   `core/review_responder/models.py:147-161`).
4. **Dispatches** one Bernstein task per round whose prompt embeds the
   file path, line range, comment body, and reviewer username
   (`core/review_responder/responder.py`).
5. **Commits and replies** exactly once per round, including a
   per-round cost cap that, when breached, posts a `needs-human`
   reply and aborts (`core/review_responder/models.py:104-145`).
6. **Audits** every round with an HMAC-chained audit entry; auto-merge
   is *never* triggered (`core/review_responder/__init__.py:18-22`).

---

## `bernstein review-responder` group

### `start` - print config and (optionally) serve the listener

```
bernstein review-responder start --repo owner/repo
                                 [--tunnel]
                                 [--port 8053]
                                 [--quiet-window 90]
                                 [--cost-cap 2.50]
                                 [--foreground]
```

Constructs a `ResponderConfig`, prints what the daemon *would* run
with, and either exits (config-printed-only) or, with `--foreground`,
boots a uvicorn server hosting the webhook listener
(`review_responder_cmd.py:51-137`).

Flags:

- `--repo owner/repo` *(required)* - GitHub slug to listen on.
- `--tunnel` - hint that you want to expose the local port via
  `bernstein tunnel start`. The CLI prints the suggested follow-up
  command but does **not** open the tunnel itself
  (`review_responder_cmd.py:109-113`).
- `--port` (default `8053`) - local TCP port for the webhook listener.
- `--quiet-window` (default `90` seconds) - silence period before a
  round is sealed. Lower = more rounds, higher = more comments per
  round.
- `--cost-cap` (default `$2.50`) - per-round cost ceiling. A breach
  posts a `needs-human` reply and aborts the round
  (`review_responder_cmd.py:74-80`,
  `core/review_responder/models.py:111-113`).
- `--foreground` - actually serve the listener; without this, only
  the config is printed (useful for verification before installing as
  a daemon).

`--foreground` requires the GitHub webhook secret to be set as
`$BERNSTEIN_REVIEW_WEBHOOK_SECRET` (the env var name lives in
`ResponderConfig.webhook_secret_env`,
`core/review_responder/models.py:130`). Without the secret the CLI
exits with a clear error (`review_responder_cmd.py:118-119`).

### `status` - show persisted dedup state

```
bernstein review-responder status [--pr <pr_number>]
```

Reads the dedup queue at the path returned by
`DEFAULT_STATE_PATH` (`core/review_responder/dedup.py`) and prints
each comment id with its `updated_at`, `outcome`, and `round_id`
(`review_responder_cmd.py:140-165`).

Use this to confirm which comments the daemon has already replied to.
`--pr` is informational only today: dedup records do not store PR
numbers, so the option is accepted but not used as a filter
(`review_responder_cmd.py:158-161`).

### `tick` - single polling pass

```
bernstein review-responder tick --repo owner/repo [--pr N ...]
```

Runs one synchronous pass of the polling listener and prints the
count of new comments observed (`review_responder_cmd.py:168-186`).
This bypasses the quiet-window bundler entirely - it only counts
comments fetched from the GitHub API.

`tick` is meant for tests, troubleshooting, and as a last-resort
manual trigger when the webhook is misconfigured. For continuous
operation, use `start --foreground` (or wrap it in a systemd /
launchd unit, see `daemon (group)` in the CLI catalog).

---

## Trigger model

The responder reacts to **inline** review comments - comments anchored
to specific lines of a diff, not the top-level PR conversation. The
trigger is timestamp-based, not label-based: every new inline comment
on a PR that the daemon is watching is considered.

Two listeners feed the bundler:

- **`WebhookListener`** - verifies an `X-Hub-Signature-256` HMAC
  signature against the secret in `BERNSTEIN_REVIEW_WEBHOOK_SECRET`,
  normalises the payload, and queues it
  (`core/review_responder/webhook.py`).
- **`PollingListener`** - falls back to `gh api` when no tunnel is
  available. Same normaliser, same queue
  (`core/review_responder/polling.py`).

Per-comment decisions made before dispatch
(`models.py:147-161`):

- **`address`** - the comment is actionable; include in the round.
- **`dismiss_stale`** - the comment is anchored to a line that no
  longer exists (anchor SHA mismatch). The responder skips it with a
  reason logged to the audit entry.
- **`dismiss_question`** - the comment matches one of the
  `question_markers` ("can you explain", "why does", "how does this",
  etc., `models.py:132-140`). The responder posts an apology reply and
  does not dispatch a task.

Auto-merge is **never** triggered. Even on a successful round, the
responder posts a commit + reply and leaves the merge decision to the
maintainer.

---

## Response cycle

For one quiet-window round:

1. **Collect.** Every inline comment that arrives within
   `quiet_window_s` of the previous comment joins the open round
   (`bundling.py`).
2. **Seal.** When the quiet window elapses, `RoundBundler` freezes the
   round and emits a `ReviewRound`
   (`core/review_responder/models.py:74-101`).
3. **Decide.** Per comment, classify as `address` /
   `dismiss_stale` / `dismiss_question`.
4. **Cap.** Rounds larger than `max_comments_per_round` (default `25`,
   `models.py:144`) split into `ceil(N/25)` follow-up rounds so a
   single huge review does not blow up cost or context.
5. **Dispatch.** A Bernstein task is created with a prompt that
   embeds the file/line/comment/reviewer for every actionable
   comment. The task runs through the normal orchestrator (gates,
   cascade routing, WAL).
6. **Watch the cap.** Cumulative cost is checked against
   `per_round_cost_cap_usd`. A breach posts a `cost_cap_breached`
   outcome (`models.py:14-26`) and aborts the round.
7. **Commit + reply.** On success the round produces exactly one
   commit and one reply per addressed comment thread. Outcome is one
   of `committed`, `needs_human`, `no_op`, `dismissed_stale`,
   `dismissed_question`, `cost_cap_breached`, `error`
   (`models.py:14-26`).
8. **Audit + dedup.** The HMAC-chained audit log records the round
   ID, outcome, commit SHA, cost, addressed/dismissed comment IDs.
   The dedup queue is updated so the same comment is never addressed
   twice
   (`core/review_responder/dedup.py`).

---

## Configuration

Tunables for the daemon live in `ResponderConfig`
(`core/review_responder/models.py:104-145`). Defaults shown:

| Setting                   | Default                              | Notes                                                                            |
| ------------------------- | ------------------------------------ | -------------------------------------------------------------------------------- |
| `repo`                    | required                             | `owner/repo` slug.                                                               |
| `quiet_window_s`          | `90.0`                               | Quiet window before sealing a round.                                             |
| `per_round_cost_cap_usd`  | `2.50`                               | Hard ceiling per round.                                                          |
| `adapter`                 | `claude`                             | One of `claude` / `codex` / `gemini` / `aider` / `generic`.                      |
| `webhook_secret_env`      | `BERNSTEIN_REVIEW_WEBHOOK_SECRET`    | Env var name; the secret value lives there.                                      |
| `polling_interval_s`      | `60.0`                               | How often `PollingListener` polls when no tunnel is active.                      |
| `question_markers`        | (8 phrases)                          | Substrings that flag a comment as a discussion question.                         |
| `listen_host`             | `127.0.0.1`                          | Bind host. Tunnel forwards public traffic to this.                               |
| `listen_port`             | `8053`                               | Bind port. Override with `--port`.                                               |
| `max_comments_per_round`  | `25`                                 | Splits oversized rounds into follow-ups.                                         |

Choosing the model:

- The adapter selects which CLI agent runs the round
  (`models.py:129`). Cost depends on the adapter's underlying provider
  and any cascade-router escalation that happens during the run. See
  `operations/MODEL_POLICY.md` for provider constraints and
  `architecture/model-routing.md` for routing details.
- `per_round_cost_cap_usd` is enforced by the cost tracker shared with
  the rest of Bernstein. There is no separate review-responder budget.
  A cap breach is final for that round - there is no auto-retry on a
  larger budget.

GitHub auth: webhook delivery is HMAC-verified using the env-secret
above. The tunnel transport (e.g. `cloudflared`) is supplied by
`bernstein tunnel start` and is not configured here.

Persistence: dedup records, round audit entries, and metrics counters
(`review_responder_comments_addressed_total`,
`review_responder_rounds_total`) all live in the project's `.sdd/`
tree. Restarting the daemon does **not** re-address comments that
have already been processed (that is the dedup queue's job).

---

## Cross-link: autofix daemon

The review responder handles the "review comments left on a PR" half
of the maintenance loop. The complementary half - "CI failed on the
PR, retry the failure" - lives in the
[autofix daemon](autofix.md) (sources:
`core/autofix/`, `cli/commands/autofix_cmd.py`).

A typical setup runs both:

- `bernstein autofix start` repairs broken CI on Bernstein PRs.
- `bernstein review-responder start --foreground` answers reviewer
  comments on the same PRs.

Together they let a maintainer treat Bernstein PRs the way they treat
PRs from a junior engineer: review what came in, leave comments, walk
away. The next round closes them out.

---

## Code pointers

- `cli/commands/review_responder_cmd.py:46` - `@click.group("review-responder")`.
- `cli/commands/review_responder_cmd.py:51-137` - `start` (config print
  + `--foreground` uvicorn boot).
- `cli/commands/review_responder_cmd.py:140-165` - `status` (dedup
  queue dump).
- `cli/commands/review_responder_cmd.py:168-186` - `tick` (one
  polling pass).
- `core/review_responder/__init__.py:1` - public surface (re-exports).
- `core/review_responder/models.py:14` - `RoundOutcome` enum.
- `core/review_responder/models.py:30` - `ReviewComment` dataclass.
- `core/review_responder/models.py:74` - `ReviewRound` dataclass.
- `core/review_responder/models.py:104-145` - `ResponderConfig`
  dataclass (all tunables).
- `core/review_responder/models.py:147-161` - `CommentDecision`.
- `core/review_responder/webhook.py` - `WebhookListener` (HMAC verify
  + normalise + queue).
- `core/review_responder/polling.py` - `PollingListener` (gh-api
  fallback).
- `core/review_responder/normaliser.py` - `normalise_webhook_payload`,
  `normalise_polling_payload`.
- `core/review_responder/bundling.py` - `RoundBundler` (quiet-window
  collapsing).
- `core/review_responder/dedup.py` - `DedupQueue`,
  `DEFAULT_STATE_PATH`.
- `core/review_responder/responder.py` - `ReviewResponder` (round
  dispatch + commit + reply).
- `core/review_responder/metrics.py` -
  `review_responder_comments_addressed_total`,
  `review_responder_rounds_total`.
