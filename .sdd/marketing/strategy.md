# Awesome-List Promotion Strategy

## Repos to promote

### Bernstein
- **URL**: https://github.com/chernistry/bernstein
- **Pitch**: AI agent orchestrator — one command spawns parallel coding agents (Claude Code, Codex, Gemini), verifies output with tests, commits clean code. Deterministic Python coordination, zero LLM tokens wasted on orchestration.
- **Categories**: agent orchestration, AI coding, CLI tools, vibe coding, LLM apps

### RightLayout
- **URL**: https://github.com/chernistry/RightLayout
- **Pitch**: AI keyboard layout fixer for macOS — auto-corrects wrong layout (EN/RU/HE) as you type. Local CoreML, no cloud, learns your habits.
- **Categories**: macOS apps, productivity, keyboard tools

---

## Pre-Submission Reconnaissance Protocol (MANDATORY)

> Every PR rejection burns social capital with the maintainer and looks spammy.
> One rejected PR costs more than 5 un-submitted ones. Recon first, submit second.

### Phase 1 — Repo Classification (GATE: determines if we proceed)

Before touching a fork, answer these questions:

| # | Check | How to verify | Kill signal |
|---|-------|---------------|-------------|
| 1 | **Repo type** — Is this a curated link list or a code showcase? | Read README top 50 lines. Check if entries are `- [Name](url) — description` links OR if they point to subdirectories with actual code | Code showcase → SKIP unless we can contribute actual code (Arindam200 case) |
| 2 | **Self-promotion policy** | Read CONTRIBUTING.md + README header for "no self-promotion", "must not be author" rules | Explicit ban → SKIP |
| 3 | **Quality gates** | Check CONTRIBUTING.md for min stars, min age, documentation requirements | Our repo doesn't meet stated minimums → SKIP |
| 4 | **Active maintainer** | `gh pr list -R OWNER/REPO --state merged --limit 3 --json mergedAt` — when was last merge? | No merges in 6+ months → deprioritize (PR will rot) |
| 5 | **Project already listed** | `grep -i "bernstein\|chernistry" README.md` | Already listed → SKIP |
| 6 | **Rejection history** | `gh pr list -R OWNER/REPO --state closed --limit 10` — look for patterns | Mass rejections of similar adds → reconsider |

**If any kill signal fires → do not submit. Move to next target.**

### Phase 2 — Format Matching (prevents formatting rejections)

| # | Check | How |
|---|-------|-----|
| 7 | **Entry format** | Copy-paste 3 existing entries from the target section. Match: emoji/no-emoji, dash/colon, link style, description length |
| 8 | **Section choice** | Read all section headers. Pick the one where our project is the best fit. Never create new sections |
| 9 | **PR template** | Check `.github/PULL_REQUEST_TEMPLATE.md`. If exists, fill every field |
| 10 | **Commit message style** | `git log --oneline -5` on the target repo. Match their commit style exactly |
| 11 | **Alphabetical ordering** | Check if entries in the target section are alphabetically sorted. If so, insert in correct position |

### Phase 3 — Content Integrity (prevents wrong-project and identity errors)

| # | Check | How |
|---|-------|-----|
| 12 | **Correct project URL** | ALWAYS use exact URL: `https://github.com/chernistry/bernstein` or `https://github.com/chernistry/RightLayout`. NEVER search GitHub for the project name — there are other projects called "Bernstein" |
| 13 | **Description accuracy** | Description must reflect OUR project's actual features, not a similarly-named project |
| 14 | **Org identity** | Fork under `chernistry-promo` org, PR from `chernistry` account. Never mix up |
| 15 | **No duplicate branches** | Before creating branch, check: `git branch -r \| grep add-bernstein`. If exists, inspect before overwriting |

### Phase 4 — Risk Assessment (score 1-5, submit only if ≥3)

| Factor | Weight | 1 (bad) | 5 (good) |
|--------|--------|---------|----------|
| Fit quality | 3× | stretch/tangential | exact category match |
| Maintainer activity | 2× | dormant | merges weekly |
| Repo visibility | 1× | <100 stars | >10K stars |
| Rejection risk | 2× | strict rules, many rejections | open/welcoming |
| Format complexity | 1× | special format/code required | simple link list |

**Score = Σ(factor × weight) / Σ(weights)**
Submit if score ≥ 3.0. Scores 2.5-3.0: submit with extra care. Below 2.5: skip.

---

## Failure Mode Taxonomy (learned from experience + data analysis)

| # | Failure Mode | Example | Prevention | Severity |
|---|-------------|---------|------------|----------|
| F1 | **Repo is code showcase, not link list** | Arindam200/awesome-ai-apps requires project source code, not README links | Phase 1, Check #1 | FATAL |
| F2 | **Wrong project referenced** | Agent found `farfarawaylabs/bernstein_ai_framework` instead of `chernistry/bernstein` | Phase 3, Check #12 — hardcode URLs, never search | FATAL |
| F3 | **Zero social proof** | Bernstein has 0 stars → maintainers silently close PRs for no-traction repos (seen across e2b, kyrolabs, jamesmurdza) | Get real stars/forks before targeting high-value repos | HIGH |
| F4 | **Self-promotion ban** | Some repos explicitly ban author submissions | Phase 1, Check #2 | FATAL |
| F5 | **Quality gate not met** | Min stars, min age, documentation requirements | Phase 1, Check #3 | FATAL |
| F6 | **Format mismatch** | Wrong emoji, wrong link style, wrong section | Phase 2, Checks #7-11 | MEDIUM |
| F7 | **Stale maintainer** | PR sits open 3+ months, never reviewed | Phase 1, Check #4 | LOW (just wastes time) |
| F8 | **Duplicate PR/branch** | Agent creates duplicate while manual PR exists | Phase 3, Check #15 | MEDIUM |
| F9 | **Looks bot-generated** | e2b-dev/awesome-ai-agents: author admitted "created by automated tool" → instant close | Personalize PR body, vary style, never batch-submit | HIGH |
| F10 | **Multiple PRs from same author** | e2b-dev: one user submitted 5+ PRs for same tools → all closed | One PR per repo, never re-submit without changes | HIGH |
| F11 | **Org name suspicion** | "chernistry-promo" name raised flags with some agents/maintainers | Consider renaming org if pattern repeats | MEDIUM |
| F12 | **PR template ignored** | Repo has specific PR template we didn't fill | Phase 2, Check #9 | MEDIUM |
| F13 | **Generic PR title** | "Update README.md" → fast path to rejection (kyrolabs pattern) | Always use "Add ProjectName — short description" | MEDIUM |
| F14 | **Category stretch** | ShopSavvy MCP Server in "Automation" of an agents list | Phase 2, Check #8 — only exact fits | MEDIUM |
| F15 | **API PR creation blocked** | hesreallyhim/awesome-claude-code blocks non-collaborator PR creation via API (GraphQL + REST both fail 404/403) | Must create PR via browser for these repos | LOW |

### ⚠️ Critical Risk: Social Proof Gap

**Bernstein: 0 stars, 0 forks (as of 2026-03-28)**

Data from closed PR analysis shows maintainers apply an **implicit traction filter** — brand-new repos with zero social proof get silently closed. This affects ALL our open PRs.

**Mitigation priority:**
1. Get early stars (friends, community, personal accounts)
2. Smaller/newer awesome-lists (< 500 stars) are more lenient — our Tier 1 small repos are safest
3. High-value repos (e2b 27K, awesome-mac 101K) will likely require visible traction to merge
4. Consider timing: let stars accumulate before targeting Tier 3 repos

---

## Placement strategy
- Add near top of relevant section (not first line — suspicious; 2nd-5th position looks organic)
- Match exact format of existing entries (emoji, link style, dash style)
- Write description that stands out but matches tone
- If alphabetical: respect ordering

## PR strategy
- One PR per repo, one entry per PR
- Professional commit message matching target repo style
- PR body: short, factual, link to repo
- Don't mention automation or self-promotion
- Follow any required PR templates
- **Always verify the diff before pushing** — confirm URL is chernistry/bernstein, not some other project

## Agent delegation rules
- **Never let agents search for "Bernstein" on GitHub** — provide exact URL
- **Never let agents skip CONTRIBUTING.md** — it's the #1 source of rejection rules
- **Always review agent PRs before they go live** — or create manually for high-value targets
- **One agent per PR max** — prevents duplicate race conditions
- **High-value repos (>10K stars): always create manually** — too much at stake for agent errors

---

## Priority tiers

### Tier 1 — Perfect fit, submit immediately
| Target | Stars | Fit | For |
|--------|-------|-----|-----|
| andyrewlee/awesome-agent-orchestrators | 129 | exact | Bernstein |
| bradAGI/awesome-cli-coding-agents | 87 | exact | Bernstein |
| EuniAI/awesome-code-agents | 92 | exact | Bernstein |
| hesreallyhim/awesome-claude-code | 33K | strong | Bernstein |
| jaywcjlove/awesome-mac | 101K | strong | RightLayout |

### Tier 2 — Good fit, submit in wave 2
| Target | Stars | Fit | For |
|--------|-------|-----|-----|
| caramaschiHG/awesome-ai-agents-2026 | 178 | good | Bernstein |
| jim-schwoebel/awesome_ai_agents | 1.5K | good | Bernstein |
| filipecalegario/awesome-vibe-coding | 3.7K | good | Bernstein |
| serhii-londar/open-source-mac-os-apps | 48K | good | RightLayout |

### Tier 3 — Stretch, submit if Tier 1-2 go well
| Target | Stars | Fit | For |
|--------|-------|-----|-----|
| Shubhamsaboo/awesome-llm-apps | 103K | stretch | Bernstein |
| nibzard/awesome-agentic-patterns | 4K | partial | Bernstein |
| Axorax/awesome-free-apps | 3.5K | partial | RightLayout |
| phmullins/awesome-macos | 3K | good | RightLayout |

### Rejected targets (with reasons)
| Target | Stars | Reason for rejection |
|--------|-------|---------------------|
| Arindam200/awesome-ai-apps | 9.4K | Code showcase, not link list — requires actual project source |
| iCHAIT/awesome-macOS | 18K | Maintainer closed PR #743, no explanation |
