# Tier-3 OpenRouter shadow-mode escalation

**Status: retired in CI.** The Tier-2 CI fixer workflow and the Tier-3
shadow job it hosted have been removed, so nothing schedules a Tier-3
capture automatically any more. The escalation logic itself still ships
in `src/bernstein/core/autofix/tier3.py` and
`scripts/run_tier3_shadow.py`, and the sections below describe how that
code behaves when it is invoked directly.

**TL;DR.** Tier-3 picked up the failing-on-main cases that Tier-1
(`contract-drift-autofix.yml`) and the retired Tier-2 CI fixer both
produced nothing on. It runs a free-tier OpenRouter model under
the `bernstein run --cli qwen` adapter, captures a unified-diff plus
a lineage / decision-log / envelope row, and **exits without
pushing**. Promotion stays governed by a second env var that is off
by default until the shadow-week metrics review lands.

| Layer | Status | Source |
|-------|--------|--------|
| Feature flag | Off by default | `vars.BERNSTEIN_CI_SELF_DRIVE` |
| Promotion gate | Off by default | `vars.BERNSTEIN_CI_SELF_DRIVE_PROMOTE_FROM_SHADOW` |
| Provider call | OpenRouter free-tier | `scripts/run_tier3_shadow.py` |
| Capture | `.sdd/autoheal/tier3-shadow/<run_id>.diff` | `core.autofix.tier3` |
| Cordon | Auto-heal cordon + `tests/contract/contracts/*.yaml` | `core.autoheal.cordon` |
| Lineage | Lineage-v2 child body, identical shape to real heal commits | `core.autoheal.lineage_writer` |
| Decision log | `tier3_shadow` / `cordon_violation` / `recurrence_escalation` | `core.observability.decision_log` |
| Cost envelope | `quota_envelope="ci-autoheal"`, daily hard cap 0 USD by default | `core.cost.cost_rollup_by_envelope` |

## Environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `BERNSTEIN_CI_SELF_DRIVE` | unset | Tier-3 fires only when this equals `tier3`. Any other value is a no-op. |
| `BERNSTEIN_CI_SELF_DRIVE_PROMOTE_FROM_SHADOW` | unset | When equal to `1`, a captured patch is signalled for push. Stays off until the shadow-week metrics review. |
| `BERNSTEIN_OPENROUTER_BASE_URL` | unset | OpenRouter base URL override. The shipped wheel never bakes a default hostname; this var (or `OPENAI_BASE_URL`) must be set in the workflow env. |
| `OPENAI_BASE_URL` | unset | Fallback when `BERNSTEIN_OPENROUTER_BASE_URL` is unset so the qwen / codex CLIs work without rewiring. |
| `OPENROUTER_API_KEY_FREE` | unset | OpenRouter free-tier API key. Read from env in the workflow only - never placed on argv. |
| `BERNSTEIN_CI_AUTOHEAL_HARD_CAP_USD` | `0.0` | Daily hard cap on the `ci-autoheal` quota envelope. Stays at zero by default because the fallback list is all `:free` models; an operator opts into a paid fallback by raising this. |

## Fallback model order

Tier-3 walks the list in the order below until one model accepts the
call. All entries are OpenRouter free-tier ids so the per-call dollar
accounting stays at zero by default. The list is documented as
operator-tunable; the defaults live in
`src/bernstein/core/autofix/tier3.py`.

1. `qwen/qwen3-coder-480b:free` (primary)
2. `deepseek/deepseek-r1:free`
3. `meta-llama/llama-4-maverick:free`
4. `mistralai/devstral-small-2:free`
5. `qwen/qwen3-235b-a22b-instruct:free`

## Cordon

A captured patch is accepted only when every touched path lands in
one of:

- The standard auto-heal cordon (`core.autoheal.cordon`): root-level
  config / docs files (`typos.toml`, `AGENTS.md`, `CLAUDE.md`,
  `.goosehints`, `CONVENTIONS.md`) and `.cursor/rules/*.mdc`.
- The Tier-3 extra glob `tests/contract/contracts/*.yaml`, added so
  contract-drift fixtures can be regenerated in shadow mode.

Anything outside that union is a hard refusal. The runner emits a
`cordon_violation` decision-log row that names the offending paths,
drops the patch, and exits without writing a diff. The cordon-zone
question itself stays governance, not engineering - widening it
requires an operator-approved RFC.

The cordon walks every file-pair header in the unified diff, not only
the new-side `+++ b/<path>` line:

- **Deletions.** A pure deletion has `+++ /dev/null`; the old-side
  `--- a/<path>` is the file the patch would delete and must pass the
  cordon. Without this the patch could delete an arbitrary
  out-of-cordon file silently.
- **Renames.** Both the old-side `--- a/<old>` and new-side
  `+++ b/<new>` paths must pass the cordon. A rename that moves a
  cordoned file outside the cordon (or pulls an out-of-cordon file
  in) is refused. The decision-log entry records both sides under
  `touched_paths` and the offending side(s) under `rejected_paths`.

## How to read shadow captures

Every Tier-3 capture writes four artefacts under `.sdd/`:

| Path | Format | Notes |
|------|--------|-------|
| `autoheal/tier3-shadow/<run_id>.diff` | unified diff | The patch the model proposed. Apply with `git apply` for a local replay. |
| `autoheal/ci-autoheal-envelope.jsonl` | JSONL | One row per capture under `quota_envelope="ci-autoheal"`. Picked up by `cost_rollup_by_envelope`. |
| `autoheal/recurrence.jsonl` | JSONL | One row per capture keyed by `(failure_class, failing_test_nodeid)`. Used by the recurrence detector. |
| `runtime/decisions.jsonl` | JSONL | The structured decision-log entry for the capture / refusal / escalation. Discoverable via `bernstein decisions tail`. |

The workflow artefact `tier3-shadow-<short_sha>` ships all four paths
together. Retention is 14 days; the operator review process is
expected to land before that window expires.

## Recurrence escalation (Tier-4 hand-off)

When the same `(failure_class, failing_test_nodeid)` pair has been
captured more than the configured threshold (`DEFAULT_RECURRENCE_THRESHOLD`,
currently `2`) inside the rolling window (`DEFAULT_RECURRENCE_WINDOW_SECONDS`,
24h), Tier-3 stops and emits a `recurrence_escalation` decision-log
entry instead of running the provider. The workflow surface is
expected to read the kind and route to the operator via the existing
Tier-4 contract (Telegram + GH issue). The recurrence ledger itself
is append-only so longer-term recurrence stats survive the window
boundary.

## Promotion path

The captured patch is **not** pushed by Tier-3 itself. Promotion is
controlled by a second env var,
`BERNSTEIN_CI_SELF_DRIVE_PROMOTE_FROM_SHADOW`. Until the shadow-week
metrics review concludes:

- The variable stays unset on the canonical repo.
- Captured patches are reviewed out-of-band by an operator looking at
  `.sdd/autoheal/tier3-shadow/` and the matching decision-log row.
- Any decision to flip the gate is documented as a separate operator
  RFC; the engineering surface is already in place.

When promotion is enabled, the Tier-3 outcome surfaces a `promoted_push`
kind instead of `shadow_captured`. The actual push (branch creation,
PR open) still flows through the standard auto-heal branch convention
(`auto-heal/<short_sha>`) so the existing required-status-checks gate
on branch protection makes the final call.

## Adversary role pre-merge gate

Even once promotion is enabled, a Tier-3 promoted patch must still
flow through the adversary role pre-merge gate
(`templates/roles/adversary/`) before merge. The shadow-mode capture
does **not** bypass the gate - it predates the push entirely, so the
gate fires on the proposed PR like any other change. If the adversary
integration needs explicit wiring (e.g. a workflow that posts the
captured diff to the adversary check before opening the PR), that
remains a follow-up tracked under `Refs #1711`.

## Cost envelope wiring

Every Tier-3 capture appends one row to
`.sdd/autoheal/ci-autoheal-envelope.jsonl`. The row carries the
canonical `quota_envelope="ci-autoheal"` tag, so the existing
`cost_rollup_by_envelope` job picks it up without configuration. The
daily hard cap defaults to `0.0` USD because every model in the
fallback list is `:free`; an operator who wires a paid fallback opts
in by setting `BERNSTEIN_CI_AUTOHEAL_HARD_CAP_USD`. The hard cap then
flows through the existing #1330 circuit breaker via the same
envelope name.

## Disabling Tier-3 in an emergency

Three independent kill switches exist; any one is sufficient.

| Action | Effect |
|--------|--------|
| Clear `vars.BERNSTEIN_CI_SELF_DRIVE` | The workflow job's `if:` gate stops it from firing. No code changes. |
| `BERNSTEIN_AUTOHEAL_DISABLE_LLM=1` | The shared auto-heal cost-guard refuses every LLM call regardless of tier. |
| Touch `.sdd/autoheal-disabled` on the runner | The existing kill-switch primitive halts every tier including Tier-3. |
