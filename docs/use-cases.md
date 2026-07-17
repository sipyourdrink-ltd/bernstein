---
search:
  boost: 2
---

# Who Uses Bernstein

These are honest workflow patterns pulled from Bernstein's own docs and CLI surface. No invented companies, no fake testimonials - just the jobs teams reach for when they want orchestration, isolation, and verification in one tool.

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
