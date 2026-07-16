# Permission modes

Bernstein gives every spawned agent a permission mode that decides which
tool calls run, which need approval, and which are blocked outright. You
pick the mode once at startup; it stays fixed for the lifetime of the run
and is applied consistently to every rule evaluation and approval gate.

This page is the operator's guide to choosing the right mode. If you
just want a one-line answer:

- **Local hacking, you trust the agent**: `bypass`
- **Read-only review of an agent's plan**: `plan`
- **Headless / CI / scheduled runs**: `auto`
- **Interactive run on a fresh repo**: `default` (this is the default)

---

## The four modes at a glance

The modes form a strict ordering from most permissive to most
restrictive. Critical rules are **never** relaxed in any mode.

| Mode      | Rank | One-liner                                                          |
|-----------|:---:|---------------------------------------------------------------------|
| `bypass`  | 0   | Skip all approvals. Only critical-severity rules still apply.       |
| `plan`    | 1   | Enforce critical+high rules. Useful for dry-runs and plan reviews.  |
| `auto`    | 2   | Enforce critical+high+medium rules. The non-interactive default.    |
| `default` | 3   | Enforce every rule, ask on anything ambiguous. The interactive default. |

### `bypass` - most permissive

Only critical-severity rules are enforced. High/medium/low rules are
relaxed to `allow`. The approval gate is **skipped** at task completion
(`bypass_enabled=True`). This is the mode behind the legacy
`--dangerously-skip-permissions` flag.

Use when: you're running on your own dev box, you trust the agent and
the goal, and you want to see what it does without prompts. Do **not**
use in CI, on shared machines, or against repos that contain secrets
your agent shouldn't touch.

### `plan` - review-friendly

Enforces critical and high rules, relaxes medium and low. Most
destructive things are still gated, but quality-of-life prompts get out
of the way.

Use when: you want to see an agent's plan and a small amount of
exploratory tool use without the full approval ceremony. The legacy
`--plan` flag and `plan_mode: true` config both map to this mode.

### `auto` - the headless default

Enforces critical, high, and medium rules; only low-severity rules are
relaxed. No legacy flag - this is what an orchestrator picks by default
when no operator is at the keyboard.

Use when: a scheduled job, CI runner, or automation harness drives the
orchestrator. The mode protects against the most common destructive
mistakes while keeping prompts to a minimum.

### `default` - the interactive default

Every rule is enforced as written. Tool calls that match no rule fall
through to `ask`, escalating to a human prompt. Approval gates run as
designed.

Use when: an operator is at the keyboard, the agent is new to the repo,
or the goal involves anything reversible. This is the safest setting
and what new users should start on.

---

## When to use which - decision matrix

| Situation                                              | Recommended mode |
|--------------------------------------------------------|------------------|
| First time running an agent in a repo                  | `default`        |
| You want to inspect the plan before any tool runs      | `plan`           |
| Headless run from CI / cron / scheduler                | `auto`           |
| Local dev box; you trust the agent and the goal        | `bypass`         |
| Repo holds secrets the agent should not touch          | `default`        |
| You hit "ask" prompts every few seconds and it's fine  | `default`        |
| You hit "ask" prompts every few seconds and it's not   | step down to `auto` |
| You disabled all rules and still see prompts           | check hooks (next section) |

Rule of thumb: pick the strictest mode that lets the agent finish
without you intervening every minute. Climbing past `auto` should be a
deliberate choice, not a reaction to friction.

---

## How a tool call gets resolved

When an agent invokes a tool, Bernstein walks four steps. The mode
participates in step 2.

1. **Match a rule.** `PermissionRuleEngine` walks the rule list in
   declaration order. First match wins. No match falls through to
   `default_for_no_match(mode)` (`default` → `ask`; everything else →
   `allow`).
2. **Apply mode relaxation.** The matched rule has an action
   (`allow`/`ask`/`deny`) and a severity. The compatibility matrix
   below converts it into an *effective action*.
3. **Resolve hooks.** `PermissionResolutionMatrix` combines the
   effective action with whatever any hooks returned (`allow` /
   `deny` / `neutral`). Hooks can restrict an `allow`, but they
   cannot override a `deny` or bypass an `ask`.
4. **Apply outcome.** `ALLOW` → tool executes. `ASK` → human prompt
   (interactive only). `DENY` → tool blocked.

### Mode × severity → enforced?

| Mode      | critical | high     | medium   | low      |
|-----------|----------|----------|----------|----------|
| `bypass`  | enforced | relaxed  | relaxed  | relaxed  |
| `plan`    | enforced | enforced | relaxed  | relaxed  |
| `auto`    | enforced | enforced | enforced | relaxed  |
| `default` | enforced | enforced | enforced | enforced |

`Relaxed` means the rule's action is overridden to `allow`. **Critical
rules are never relaxed**, regardless of mode.

### Hook resolution rules

Once you have an effective action and a hook outcome:

1. Effective rule = `DENY` → **DENY** (hooks cannot override)
2. Effective rule = `ASK` → **ASK** (hooks cannot bypass humans)
3. Effective rule = `ALLOW` + hook = `DENY` → **DENY**
4. Effective rule = `ALLOW` + hook = `ALLOW`/`NEUTRAL` → **ALLOW**
5. No rule + hook = `DENY` → **DENY**
6. No rule + hook = `ALLOW` → **ALLOW**
7. No rule + hook = `NEUTRAL` → **ASK** (default to safety)

If a tool call surprises you, the resolution chain above is the
shortest path to a useful answer.

---

## Configuration

### YAML (`bernstein.yaml`)

```yaml
permission_mode: auto       # bypass | plan | auto | default
```

If absent or `null`, the orchestrator falls back to `default` and logs
a warning when an unrecognised value is supplied.

### CLI flag

```bash
bernstein run --permission-mode auto
```

### Legacy flag mapping

The orchestrator still accepts older flags and quietly maps them:

| Legacy flag / config value           | Canonical mode |
|--------------------------------------|----------------|
| `--dangerously-skip-permissions`     | `bypass`       |
| `dangerously_skip_permissions: true` | `bypass`       |
| `--plan` / `plan_mode: true`         | `plan`         |
| `--auto` / no flag (orchestrator)    | `auto`         |
| (interactive CLI, no flag)           | `default`      |

`resolve_mode()` checks canonical names first, then legacy names, then
falls back to `default` with a warning.

---

## Worked example

You run an agent in `auto` mode. Your `.bernstein/rules.yaml` says
(rules live in a flat list under the `permission_rules:` key; `id` and
`action` are required, and a rule without them is skipped):

```yaml
# .bernstein/rules.yaml
permission_rules:
  - id: deny-rm-root
    action: deny
    severity: critical
    tool: Bash
    command: "rm -rf /*"
  - id: ask-rm-recursive
    action: ask
    severity: high
    tool: Bash
    command: "rm -rf *"
  - id: ask-write
    action: ask
    severity: low
    tool: Write
```

See [security-hardening.md](../security/security-hardening.md) for the
full field reference (`id`, `action`, `severity`, `tool`, `command`,
`path`, `description`).

The agent calls:

- `Bash("rm -rf /")` → matches rule 1, severity critical, mode does
  not relax → **DENY**.
- `Bash("rm -rf *")` → matches rule 2, severity high, `auto` enforces
  high → **ASK** the operator.
- `Write("README.md", ...)` → matches rule 3, severity low, `auto`
  relaxes low → **ALLOW**.

Switch to `default` and rule 3 stays `ask`. Switch to `bypass` and
rule 1 still denies, rule 2 relaxes to `allow`, rule 3 relaxes to
`allow`.

---

## Code pointers

| File                                                    | What it does |
|---------------------------------------------------------|--------------|
| `src/bernstein/core/security/permission_mode.py`        | Canonical `PermissionMode` enum, `resolve_mode()`, compatibility matrix |
| `src/bernstein/core/security/permission_rules.py`       | `PermissionRuleEngine` - matches rules, applies mode relaxation |
| `src/bernstein/core/security/permission_matrix.py`      | `PermissionResolutionMatrix` - combines rule outcome with hook outcome |
| `src/bernstein/core/security/approval.py`               | Approval gate - honours `bypass_enabled` when mode is `bypass` |
| `tests/unit/test_permission_mode.py`                    | 62 tests covering every cell of the matrix |

---

## Related

- [Sandbox backends](sandbox.md) - what an agent can reach even when
  permissions allow a call.
- [`operations/runbooks.md`](../operations/runbooks.md) - automated
  remediation for the kinds of failures permissions are meant to
  prevent in the first place.
