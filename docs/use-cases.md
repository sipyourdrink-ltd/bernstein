---
search:
  boost: 2
---

# Who Uses Bernstein

These are honest workflow patterns pulled from Bernstein's own docs and CLI surface. No invented companies, no fake testimonials - just the jobs teams reach for when they want orchestration, isolation, and verification in one tool.

## Who this is for

Specific shapes where the value lands:

- engineering teams running >=3 CLI coding agents in parallel: each agent gets its own git worktree, the merge queue serialises landings, no race conditions
- operators running compliance-sensitive workflows: every routing decision is plaintext, and with `--audit` the log is HMAC-signed and tamper-evident, no SaaS hop, no third-party data plane
- platform teams that need an audit log of agent decisions: enable `--audit` and the orchestrator writes one row per scheduling decision, you can grep it
- anyone burning more than $1k/mo on coding agents who wants determinism: you can replay yesterday's plan and get yesterday's task graph
- forward-deployed engineers dropping into a client repo: credentials stay in your env, not the client's; agents you spawn are whichever CLI tool the client already trusts
- teams whose pipeline also produces non-code deliverables (a report, a dataset, an ops result): a task can complete on a signed lineage receipt instead of a git commit; see the [artifact contract](operations/artifacts.md) page for what is reachable today

If you nodded at two of those bullets, this fits.

## Who this is NOT for

- "I want one pair-programmer to chat with about my code": a single CLI agent is fine. Bernstein adds orchestration overhead you don't need.
- prototypes where merge gates are overkill: the lint/types/tests/cross-model-review pipeline is value when the cost of a bad merge is real, friction when you're throwing the repo away on Friday.
- anyone who wants a SaaS wrapper with a credit-card form: Bernstein is on-prem only by design.
- teams that need a vendor with a support SLA and a contract: solo open-source project. GitHub issues are how support happens.
- generic LLM chat workflows with no verifiable deliverable: a task has to produce something checkable - a commit, or an artifact whose canonical bytes a criterion can be evaluated against.
- research-shape "let the agents collaborate emergently" use cases: the deterministic scheduler is a hard wall there.

## Deployment shapes

- forward-deployed engineering: drop the crew onto a client repo when you arrive, take it with you when you leave.
- self-evolving projects: point Bernstein at its own repo and let it execute the backlog (the Bernstein codebase is one).
- CI fleets: run a crew of agents in parallel on PRs, with per-agent credential scoping and an opt-in signed audit trail.
- air-gapped deployment: install from a signed wheelhouse, run with `--profile airgap` to deny outbound by default. See [air-gap installation](installation/air-gap.md).

## Common workflows

### Parallel test generation with AI agents

When a codebase has dozens of untested modules, the bottleneck is usually coordination, not test-writing itself. Bernstein lets you fan out the work across isolated worktrees so multiple agents can add coverage at the same time without stepping on each other.

```bash
BERNSTEIN_MAX_AGENTS=5 bernstein -g "Generate unit tests for untested modules in src/"
```

Good fit when you want broad coverage quickly, but still need janitor verification before anything lands.

### CI failure repair that opens a follow-up fix

Some teams use Bernstein as the repair loop after review has already happened. The autofix daemon watches open PRs, classifies failing checks, and dispatches a scoped fix run when CI turns red.

```bash
bernstein autofix start --repo your-org/your-repo --foreground
```

Good fit when the painful part is not finding failures, but paying the context-switch tax every time lint, typing, or a focused test failure breaks a PR. See [operations/autofix.md](operations/autofix.md).

### PR review follow-up from inline comments

Review comments are often small, mechanical, and easy to lose between pushes. Bernstein's review-responder daemon turns those comments into tracked work so the author can keep moving while the system handles the obvious follow-up.

```bash
bernstein review-responder start --repo your-org/your-repo --foreground
```

Good fit when your team already does human review but wants the "please rename this / add a guard / update the test" loop to close faster. See [operations/review-responder.md](operations/review-responder.md).

### Large-scale codebase modernization

The classic migration problem is repetitive but still risky: move callbacks to async/await, add types, rename an API surface, or update a framework pattern everywhere. Bernstein helps by splitting the migration into parallel worktrees, then running verification gates before merge.

```bash
BERNSTEIN_MAX_AGENTS=8 bernstein -g "Migrate callback-based modules in src/ to async/await and update tests"
```

Good fit when the change is spread across many files and the main risk is merge conflict churn or uneven follow-through.

### Ticket-to-run execution from GitHub, Jira, or Linear

A lot of teams do not want another planning system; they want their existing tickets to become executable work. Bernstein can import a ticket URL, infer scope, and immediately turn it into a run.

```bash
bernstein from-ticket https://github.com/your-org/your-repo/issues/123 --run
```

Good fit when you want issue trackers to stay as the source of truth instead of copying requirements into a separate agent tool.

### API-change safety checks before merge

Breaking a function signature is easy; finding every downstream caller is the annoying part. `dep-impact` compares your branch against a base ref and reports affected call sites before the change merges.

```bash
bernstein dep-impact --base main
```

Good fit when you are doing refactors in shared libraries or internal platforms and want a fast, concrete answer to "what else does this break?"

## Submit your workflow

If you use Bernstein for something real, open a PR and add it here. The bar is simple: describe the workflow, include a command that actually exists, and say what worked or where it was rough.
