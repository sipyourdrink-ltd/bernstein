# Ten worktrees, ten agents, one merge train

**Published:** 2026-04-25
**Target:** Dev.to, HN follow-up, Python Weekly
**Reading time:** ~7 minutes

---

I shipped Bernstein 1.9 in a single afternoon. Ten features. One release branch. The diff is ~12,000 lines across 22 packages and 9 CLI surfaces. I wrote almost none of the code.

What I did was sit at one terminal and run ten coding agents in parallel — each in its own git worktree, each opening its own PR, each finishing without ever looking at what the others were doing. Then I merged them in dependency-safe order, fixed the conflicts that surfaced (there were three), and bumped the version to 1.9.0.

The interesting part isn't that "AI wrote the code." The interesting part is what broke when I tried to make ten of them write code at the same time on the same codebase, and what didn't.

This post is the postmortem.

---

## The setup

Bernstein already had ten ready-to-execute YAML tickets sitting in `.sdd/backlog/open/release_1.9_*.yaml`. Each is a real feature ticket with acceptance criteria, file scope, test layout, and constraints (no version bumps, no README edits, no touching peer tickets' modules).

I dispatched ten agents in one message — each running Claude Opus 4.7, each in an isolated git worktree under `.claude/worktrees/agent-<id>/`, each on its own branch `feat/release-1.9-<slug>` based on a fresh `release/1.9` integration branch. The orchestration script that did this is one Python loop. There is no LLM in it. The orchestrator is determined entirely by which YAML files I pointed at and what worktrees were free.

This is the exact pattern Bernstein implements internally. I was running it manually here only because I wanted to be the one approving each merge.

## What the ten tickets did

For context, the features (one PR each):

| PR | Module | What it ships |
|----|--------|---------------|
| #940 | `core/protocols/mcp_catalog/` | Discoverable registry of installable MCP servers |
| #941 | `core/preview/` | Sandboxed dev-server with public tunnel link |
| #942 | `core/planning/lifecycle.py` | Auto-archived plan files with run summary |
| #943 | `core/protocols/acp/` | Native Agent Client Protocol bridge for IDE clients |
| #944 | `core/fleet/` | Multi-project supervisory dashboard (TUI + web) |
| #945 | `core/quality/review_pipeline/` | YAML-driven multi-phase review pipeline DSL |
| #946 | `core/autofix/` | Daemon that auto-repairs CI failures on Bernstein PRs |
| #947 | `core/security/vault/` | OS-keychain credential vault for providers |
| #948 | `core/review_responder/` | Addresses reviewer comments on Bernstein PRs |
| #949 | `core/notifications/` | NotificationSink protocol + Telegram/Slack/Discord/Email/Webhook/Shell drivers |

Total: ~12,000 lines, ~360 new tests, ~80 new files, 9 new CLI subcommand groups, 3 new pluggy entry-point groups. Every PR opens with green local tests and a passing `ruff check`.

## What didn't break

A lot, actually.

- **Test discipline.** Every agent shipped its own `tests/unit/<area>/` and `tests/integration/<area>/`. The total test suite grew from ~2,000 to ~2,360 tests. None broke.
- **Module boundaries.** Each ticket explicitly named the directory the agent was allowed to add code under (e.g., `src/bernstein/core/preview/`). Nine of the ten agents stayed inside the sandbox.
- **Scope discipline.** No agent silently bumped the version. No agent rewrote the README. No agent touched a peer ticket's module. The "do not touch" list in the prompt held.
- **Feature wiring to existing primitives.** Every PR that needed the HMAC audit chain, the cost tracker, the sandbox backends, the lifecycle hooks, the streaming-merge utility, or the bulletin board reused them rather than reimplementing. That's not the agent being clever — that's the prompt naming the existing primitive.

## What did break

Three real things, with three honest failure modes.

### 1. Lint format vs lint check

Six of the ten PRs failed CI on the first push. All on the same job: `ruff format --check`. The agents had run `ruff check` (the linter) but not `ruff format --check` (the formatter). My ticket prompts said "ruff-clean" without distinguishing, and the agents picked the half they recognised.

Fix per PR: one `ruff format <paths>` and a follow-up commit. Total time across all six: under five minutes. But six CI runs got burned for nothing.

Permanent fix: tightening the prompt language to "run BOTH `ruff check` AND `ruff format --check` before pushing." This is the kind of thing that's obvious in retrospect and invisible until something forces it.

### 2. Worktree leakage

Three agents wrote files into the main repository working tree before realising they were supposed to be in `.claude/worktrees/agent-<id>/`. Every Bash invocation starts a fresh shell, and `cd <worktree>` from one invocation does not persist to the next. So an agent that did `cd <worktree> && some_command` once, then ran a separate Bash call without re-cd, would land its file in the wrong place.

The CLAUDE.md memory file already warned about this. The prompts repeated the warning. Three agents still got it wrong.

What saved me: the agents that noticed mid-run rebuilt their work in the correct worktree before pushing. The main repository's working tree got transient pollution but no commits leaked into the wrong branch. The contamination was real but contained.

Real fix: the next iteration of these prompts will hand each agent an explicit `WORKTREE=<absolute-path>` env var and require every Bash call to use `cd "$WORKTREE" && ...` or `git -C "$WORKTREE" ...` patterns. No relying on cwd persistence.

### 3. Three real merge conflicts

After merging the first six PRs cleanly, the seventh — the credential vault PR — surfaced a conflict in `src/bernstein/cli/main.py`. The vault agent had added a `connect_cmd` registration in the same import block where the autofix agent had added an `autofix_group` registration. Both blocks were correct in isolation; they were structurally identical edits to the same file region, so git couldn't auto-merge them.

The same conflict shape repeated for #948 (review-responder) and #949 (notifications). Total resolution time: under two minutes per conflict. Each was a "keep both" — the two added imports/registrations were independent and both belonged.

What this told me: parallel agents are bottlenecked not by their code quality but by the **convergence points** in the codebase. `cli/main.py`'s flat list of `cli.add_command(...)` calls is one. A more conflict-resistant design would split command registration into per-package files that the entry point auto-discovers, eliminating the single edit hotspot. That's a Bernstein follow-up ticket for 1.10, not a 1.9 problem.

## Numbers

I ran this as a stress test, not as a "how cheap is parallel agent execution" benchmark — I picked Opus 4.7 across the board for consistency, when several of these tickets could have ridden Sonnet or Haiku.

- **Agents dispatched:** 10
- **Wall-clock time, dispatch to all PRs open:** ~25 minutes
- **Wall-clock time, dispatch to all 10 merged into release/1.9:** ~70 minutes (including CI rerun cycles after lint fixes)
- **PRs that needed a fixup commit before merging:** 6 (lint format) + 0 (vulture had one false positive in the fleet PR; 1 line)
- **Merge conflicts:** 3, all in `cli/main.py`, all "keep both"
- **Tests added:** ~360
- **Net source LOC added:** ~12,000

The cost-aware bandit router (the thing that picks model + effort per task) wasn't engaged here. The next iteration will be — half these tickets are config and CRUD shape; Haiku would have written them faster and ten times cheaper, with no quality delta.

## What this is and isn't

It is **not** a "ten agents shipped a release autonomously" claim. I dispatched the prompts. I read every PR before merging. I resolved every conflict by hand. I wrote this post by hand. The agents wrote the code; I ran the project.

It is **not** evidence that AI-written features are production-ready by default. They aren't. Three of the ten agents still emitted lint-broken code. One emitted a vulture false positive. None caught the convergence-point design smell in `cli/main.py`. That was on me to notice.

It **is** evidence that the bottleneck in multi-agent shipping moved. With ten coding agents, the bottleneck stops being "can one agent get this right" and becomes "can the codebase absorb ten parallel writers without thrashing." Worktree isolation, deterministic dispatch, file-scope discipline in tickets, and conflict-resistant module layout matter ten times more than they did when I was the only person writing.

That's the design thesis of Bernstein, demonstrated on Bernstein. Not because the agents are magic, but because the orchestration discipline scales when individual cognition doesn't.

## What's in 1.9 you'd actually use

Not all ten features matter equally. If you skim the changelog, the ones I'd actually call out:

- **`bernstein autofix`** — daemon that watches your Bernstein-opened PRs, pulls failing CI logs via `gh run view --log-failed`, and dispatches a fresh repair run scoped to the failure. Three-attempt cap, label-gated, every attempt HMAC-audited. (PR #946)
- **`bernstein preview`** — boots the agent's dev server inside the originating sandbox, captures the bound port, exposes it through the existing tunnel wrapper with a configurable expiry and auth mode. One command from "agent finished" to shareable HTTPS link. (PR #941)
- **`bernstein connect <provider>`** — OS-keychain credential vault. Stops the slide where every Bernstein integration ends up with a token in `.env` that leaks into screen shares and shell history. (PR #947)
- **`bernstein acp serve --stdio`** — native ACP bridge so Zed and other ACP-aware editors can use Bernstein as their backend. The first IDE-native multi-agent orchestrator. (PR #943)

The other six are infrastructure that makes the first four work cleanly under load. None of them are "look at me" features individually; together they're the difference between "you can run multi-agent on a real repo" and "you can supervise a fleet of multi-agent runs across many repos."

---

**Bernstein 1.9.0 is on PyPI now: `pip install bernstein`. The full release notes are at [github.com/sipyourdrink-ltd/bernstein/releases/tag/v1.9.0](https://github.com/sipyourdrink-ltd/bernstein/releases/tag/v1.9.0).**

---

*Bernstein orchestrates short-lived AI coding agents — Claude Code, Codex, Gemini CLI, and 28 more — through a deterministic Python dispatcher. State lives in files, not in agent memory. The orchestrator is code, not a model. Apache-2.0. [github.com/sipyourdrink-ltd/bernstein](https://github.com/sipyourdrink-ltd/bernstein).*
