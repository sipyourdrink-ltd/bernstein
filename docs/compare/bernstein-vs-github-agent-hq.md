# Bernstein vs. GitHub Agent HQ

> **tl;dr** — GitHub Agent HQ was announced at GitHub Universe 2025 and rolled out through early 2026. It's excellent if you live in GitHub's ecosystem and don't mind the lock-in. Bernstein is the open-source alternative: model-agnostic, CLI-native, runs anywhere, and costs less for mixed workloads.

*Last verified: 2026-04-19. Based on GitHub's Universe 2025 announcements and the public Agent HQ rollout notes.*

---

## What Agent HQ is

Announced at GitHub Universe 2025 and rolled out to GitHub.com through early 2026, Agent HQ is a multi-agent coding system that runs Claude, Codex, and Copilot agents on the same task. It uses a coordinator agent to break down a goal, assigns subtasks to specialized agents, and shows the activity in a GitHub-native UI (issues, PRs, checks).

This is structurally close to what Bernstein does. GitHub building it validates the architectural bet: parallel short-lived agents, a deterministic coordinator, external verification before merge. The main difference is who owns the orchestrator and what it's allowed to talk to.

---

## What each tool is

**GitHub Agent HQ** is a multi-agent coding system built into GitHub. It runs inside GitHub's infrastructure, uses GitHub's AI models, and integrates tightly with pull requests, issues, code review, and GitHub Actions. The orchestrator logic is GitHub's proprietary code.

**Bernstein** is an open-source orchestrator for CLI coding agents. It runs on your machine (or in CI), wraps whichever CLI tools you have installed, and stores state in plain files. The orchestrator is ~800 lines of deterministic Python with no LLM calls on coordination. You can read, modify, and extend all of it.

---

## Feature comparison

| Feature | Bernstein | GitHub Agent HQ |
|---|---|---|
| **Open source** | Yes — Apache 2.0 | No — proprietary |
| **Model flexibility** | Any CLI agent (31 adapters) | Claude, Codex, Copilot (GitHub-managed) |
| **Provider lock-in** | None | GitHub + Microsoft/Anthropic/OpenAI |
| **Runs outside GitHub** | Yes — any git repo, any host | No — GitHub-only |
| **CLI-native** | Yes — works in terminal, SSH, CI | No — GitHub web UI + API |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Cost optimization** | Bandit routing, model cascade | Fixed to GitHub pricing tiers |
| **Headless / overnight** | Yes — `--headless` flag | Limited (GitHub Actions only) |
| **Task planning** | LLM planner from natural language goal | Coordinator agent from issue/PR context |
| **Agent isolation** | Git worktree per agent, process isolation | GitHub-managed (details not public) |
| **Result verification** | External janitor (tests, linter, files) | GitHub CI + code review |
| **Multi-repo support** | Yes — workspace mode | GitHub Codespaces context |
| **GitHub integration** | Webhook adapter (planned) | Native — issue-to-PR workflow |
| **Audit trail** | `.sdd/` files, git history | GitHub activity log |
| **Self-hostable** | Yes — runs anywhere Python runs | No |
| **Enterprise pricing** | None (self-hosted, pay for model tokens only) | GitHub Copilot Enterprise pricing |

---

## Architecture comparison

**GitHub Agent HQ (GitHub-integrated):**
```
GitHub issue / PR / comment
    │
    ▼
Agent HQ coordinator (GitHub infrastructure)
    │
    ├── Agent 1 (Claude)   — GitHub-managed execution
    ├── Agent 2 (Codex)    — GitHub-managed execution
    └── Agent 3 (Copilot)  — GitHub-managed execution
         │
         ▼
    PR created, CI triggered, code review requested
```

The orchestrator lives in GitHub's cloud. Agents have native access to your repo, issues, and PR history. The output is a pull request in the normal GitHub workflow.

**Bernstein (local, CLI-native):**
```
bernstein -g "goal"  (terminal, CI, SSH)
    │
    ▼
Task server (local FastAPI, deterministic Python)
    │
    ├── Task A → claude  (isolated worktree) → janitor → merge
    ├── Task B → codex   (isolated worktree) → janitor → merge
    └── Task C → gemini  (isolated worktree) → janitor → merge

State: .sdd/ files (backlog, runtime, metrics, config)
```

The orchestrator runs on your hardware. Agents execute in isolated git worktrees with fresh context per task. The janitor runs your tests and linter before any merge. All state is plain files — no GitHub dependency.

---

## Benchmark: identical tasks, both platforms

> **Methodology note:** Direct benchmarking of Agent HQ requires GitHub access and is subject to GitHub's usage policies. Bernstein is no longer publishing estimated or simulated cross-platform benchmark tables on this page.

What we can say responsibly today:

- Bernstein has a verified-eval publication path based on `benchmarks/swe_bench/run.py eval`.
- Public v1 benchmark scope is Bernstein vs real single-agent baselines on SWE-Bench Lite.
- Agent HQ remains a qualitative comparison here until Bernstein can run a Bernstein-owned live harness against it.

### What this page compares instead

This page now focuses on product tradeoffs that do not require invented or estimated leaderboard rows:

- workflow ownership
- GitHub-native integration
- cost transparency
- model flexibility
- local control vs managed platform convenience

### Bernstein public benchmark track

When Bernstein publishes benchmark numbers publicly, the first acceptable format is:

- `Verified Pilot Results (n=50)`
- run date shown
- commit SHA shown
- sourced from `benchmarks/swe_bench/run.py eval`

GitHub has not published Agent HQ SWE-Bench results as of 2026-04-17. Until Bernstein can reproduce Agent HQ under a Bernstein-owned live harness, this page will not claim numeric wins or losses.

---

## Cost model: where the difference matters

GitHub Agent HQ cost is bundled into Copilot Enterprise ($39/user/month) or charged as additional compute. For teams already on Copilot Enterprise, Agent HQ feels "free" — it's included in the seat cost. For teams not on Copilot Enterprise, you're paying for the full subscription to access Agent HQ.

Bernstein cost = model API tokens only. No seat license, no subscription. A typical medium-complexity task using the mixed-model routing costs $0.15–0.20 in API tokens. A 10-task session costs roughly $1.50–2.00.

**When Agent HQ is cheaper:** If your team already pays for Copilot Enterprise and runs fewer than ~200 Agent HQ tasks per user per month, the incremental cost per task is effectively zero.

**When Bernstein is cheaper:** If you're not on Copilot Enterprise, if you run high task volumes, or if you route tasks aggressively to Haiku/Gemini, Bernstein wins on unit economics.

---

## Honest assessment: where Agent HQ is better

**GitHub integration is genuinely good.** Agent HQ turning a GitHub issue into a pull request, with CI triggered and reviewers requested, requires zero configuration. Bernstein can produce a PR too — `bernstein -g "fix issue #42"` — but it requires a GitHub CLI setup and a post-merge hook. If your team's workflow is issue → PR → merge and you want that to happen without touching a terminal, Agent HQ is more polished.

**No local setup.** Agent HQ runs in GitHub's cloud. No installation, no Python environment, no API key wiring. For a team that doesn't want infrastructure overhead, this is a real advantage.

**Multi-model coordination within a task.** Agent HQ can have Claude review Codex's output within the same task execution, using GitHub's context (diff, test results, comments). Bernstein's agents are isolated — they don't see each other's output mid-task by default. Cross-agent awareness requires explicit task dependencies.

**Enterprise support.** GitHub sells SLAs, security reviews, and compliance documentation. Bernstein is community-supported (Apache 2.0). If you need a vendor to sign your procurement paperwork, Agent HQ wins.

---

## When to use Agent HQ instead

- **Your workflow is entirely GitHub-native.** Issue tracking, code review, CI — all on GitHub. Agent HQ slots into this with zero friction.
- **Your team is already paying for Copilot Enterprise.** The incremental cost of Agent HQ is near zero for existing subscribers.
- **You want zero local setup.** No Python, no API keys, no terminal. Just a GitHub issue and a "Run with Agent HQ" button.
- **You need enterprise SLAs and vendor support.** GitHub offers compliance documentation and a support contract. Bernstein does not.

---

## When to use Bernstein instead

- **You host code outside GitHub.** GitLab, Bitbucket, self-hosted Gitea, or a plain git remote — Bernstein doesn't care. Agent HQ requires GitHub.
- **You want model flexibility.** Bernstein can run Claude, Codex, Gemini, and Qwen in the same session, routing each task to the cheapest capable model. Agent HQ's model selection is controlled by GitHub.
- **You want to own the orchestrator.** Agent HQ's coordination logic is a black box. Bernstein's is 800 lines of Python you can read, modify, fork, and audit.
- **You want cost transparency and optimization.** Bernstein logs every token spent per task, per model, per role. The bandit router learns which models are cheapest for each task type. Agent HQ's cost model is Copilot Enterprise billing.
- **You want self-evolution.** `bernstein --evolve` analyzes past run metrics and improves prompts, routing rules, and templates. Agent HQ has no equivalent.
- **You want headless, overnight, or CI operation.** `bernstein --headless` runs until the backlog is empty or the budget runs out. Agent HQ's async execution is limited to GitHub Actions contexts.
- **You're not on GitHub.** If this sentence applies, you're done reading.

---

## The philosophical difference

Agent HQ is GitHub saying: "multi-agent coding is the future, and we're going to run it for you inside our platform."

Bernstein is the answer to: "what if you want to run the orchestrator yourself, on your infrastructure, with your choice of models, against any git host?"

Both bets can be right simultaneously. Platform-native tools win on integration. Open-source tools win on flexibility and ownership. The same dynamic played out with CI/CD (GitHub Actions vs. self-hosted Jenkins/GitLab CI), container registries, and package hosting.

If GitHub Agent HQ is GitHub Actions, Bernstein is what you use when you can't or won't use GitHub Actions.

---

## Reproducing the Bernstein benchmarks

```bash
# Install benchmark dependencies
uv add datasets swebench

# Run the 10-task internal benchmark
uv run python benchmarks/run_benchmark.py --all

# Run SWE-Bench Lite (requires Docker + API keys, ~4 hours)
uv run python benchmarks/swe_bench/run.py \
    --scenarios bernstein-sonnet bernstein-mixed solo-sonnet solo-opus \
    --results-dir benchmarks/swe_bench/results

# Generate comparison report
uv run python benchmarks/swe_bench/run.py report \
    --results-dir benchmarks/swe_bench/results
```

Raw data and methodology: [`benchmarks/`](../../benchmarks/)

---

## See also

- [Bernstein benchmark: multi-agent vs single-agent](../../benchmarks/README.md)
- [Competitive matrix: Bernstein vs. CrewAI, AutoGen, LangGraph](../competitive-matrix.md)
- [Zero lock-in: model-agnostic orchestration](../zero-lock-in.md)
- [Full comparison index](./README.md)
