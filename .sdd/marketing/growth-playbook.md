# Bernstein & RightLayout — Growth Playbook

> Long-run promotion strategy. Updated: 2026-03-28.
> Goal: Build visible traction (stars, forks, users) to support awesome-list merges and establish Sasha as a recognized OSS author.

---

## Current State

| Metric | Bernstein | RightLayout |
|--------|-----------|-------------|
| Stars | 2 | TBD |
| Forks | 0 | TBD |
| Open PRs | 20 | 4 |
| Merged PRs | 0 | 0 |
| Key risk | Zero social proof → silent PR closures | Same |

---

## Channel Strategy

### Tier 1 — High-Impact, Do First (Week 1-2)

#### 1. Reddit Launch Posts
Target subreddits (one post per sub, spaced 2-3 days apart):

| Subreddit | Members | Post Style | For |
|-----------|---------|------------|-----|
| r/ClaudeAI | 150K+ | "I built an orchestrator that spawns parallel Claude Code agents" | Bernstein |
| r/ChatGPTCoding | 100K+ | "Open-source tool: one command → parallel AI agents build your code" | Bernstein |
| r/MachineLearning | 3M+ | [Project] flair, technical angle on deterministic orchestration | Bernstein |
| r/coding | 5M+ | "I got tired of babysitting AI agents, so I built this" | Bernstein |
| r/programming | 6M+ | Technical deep-dive, architecture diagram | Bernstein |
| r/mac | 1M+ | "I built an app that fixes wrong keyboard layout with AI" | RightLayout |
| r/macapps | 200K+ | Short demo, "free, local, no cloud" angle | RightLayout |

**Tips:**
- Use personal "I built" framing — never corporate-sounding
- Include a GIF/video demo (30s max)
- Post during US morning (9-11am ET) for max visibility
- Engage with every comment in first 2 hours
- Don't post to more than 2 subs same day (looks spammy)

#### 2. Hacker News — Show HN
- Title: "Show HN: Bernstein — Spawn parallel AI coding agents with one command"
- Post at 8-9am ET on Tuesday/Wednesday (best HN days)
- Have a compelling first comment ready with architecture details
- Must have demo GIF in README before posting
- One shot — if it doesn't hit front page, don't repost for 2+ weeks

#### 3. Twitter/X Thread
- Launch thread from @chernistry account
- Format: problem → solution → demo GIF → architecture → link
- Tag relevant accounts: @AnthropicAI, @OpenAI, @GoogleDeepMind
- Use hashtags: #AIAgents #VibeCoding #OpenSource #ClaudeCode
- Pin the thread

#### 4. Competition Discord
- Already drafted (two variants in previous session)
- Post in the competition channel with focus on Bernstein's role in RAG challenge

### Tier 2 — Sustained Growth (Week 2-4)

#### 5. Dev.to / Hashnode Article
- Title: "How I Built a Deterministic AI Agent Orchestrator (Zero LLM Tokens on Coordination)"
- Technical deep-dive with code snippets
- Cross-post to both platforms
- Link to GitHub repo prominently

#### 6. YouTube Demo (3-5 min)
- Screen recording: bernstein -g "Add user auth" → watch agents spawn → tests pass → commit
- Terminal aesthetic, no slides needed
- Post to YouTube, embed in README
- Share on Reddit/Twitter

#### 7. Product Hunt Launch
- Prep: good screenshots, tagline, maker comment
- Schedule for Tuesday 12:01am PT
- Rally community to upvote in first 4 hours
- "AI Agent Orchestrator" category

### Tier 3 — Compounding Growth (Month 2+)

#### 8. Blog Posts on alexchernysh.com
- "Building Bernstein: Architecture Decisions Behind a Multi-Agent Orchestrator"
- "Why Deterministic Orchestration Beats LLM-Based Planning"
- "Lessons from Running 50+ Parallel AI Agents in Production"
- Cross-promote via awesome-blog lists

#### 9. Conference Talks / Meetups
- Submit to local meetups (AI/ML, Python, DevTools)
- 15-min lightning talk format
- Record and post to YouTube

#### 10. GitHub Community Building
- `good-first-issues` and `hacktoberfest` tags already set
- Create 5-10 well-written issues labeled `good first issue`
- Write CONTRIBUTING.md with clear onboarding
- Respond to issues/PRs within 24h
- Add "Contributors" section to README

---

## Awesome-List Pipeline

### Current Pipeline
- **Wave 1+2**: 24 open PRs across awesome-* lists (~220K combined stars)
- **Wave 3**: Being discovered (search running)
- **Monitoring**: Mon/Thu 10am via scheduled task

### Pipeline Rules
1. **Recon before every PR** — see strategy.md Phase 1-4
2. **Stars threshold**: Don't target repos with >10K stars until Bernstein has 20+ stars
3. **Timing**: Space submissions 2-3 days apart to avoid pattern detection
4. **Quality over quantity**: 1 merged PR on a 10K-star list > 10 open PRs on 100-star lists
5. **Response**: If maintainer comments/requests changes, respond within 24h

### Conversion Funnel
```
Discovery → Recon → Fork → PR → Maintainer Review → Merge → Stars
   100%      60%    60%   100%       50%               30%    varies
```
Expected: ~7-8 merges from 24 open PRs (30% merge rate for new/low-star projects).

---

## Weekly Cadence

| Day | Activity | Time |
|-----|----------|------|
| Mon | PR status check (automated) + plan week's posts | 10am |
| Tue | Reddit post (rotate sub) or HN submission | 9am ET |
| Wed | Blog promoter runs (automated) | 11am |
| Thu | PR status check (automated) + respond to comments | 10am |
| Fri | Engage with community (comments, issues, discussions) | flexible |
| Sat | Write content (blog post, article, tweet thread) | flexible |
| Sun | Rest / review analytics | — |

---

## Success Metrics (30-day targets)

| Metric | Target | Stretch |
|--------|--------|---------|
| GitHub stars (Bernstein) | 50 | 200 |
| GitHub forks | 5 | 20 |
| Awesome-list merges | 5 | 10 |
| Reddit upvotes (total) | 100 | 500 |
| HN front page | 0-1 | 1 |
| Blog post views | 500 | 2000 |

---

## Anti-Patterns (Don't Do)

1. **Don't astroturf** — no fake stars, no sock puppet accounts
2. **Don't spam** — max 2 Reddit posts per week across all subs
3. **Don't be pushy** — if a maintainer says no, move on
4. **Don't automate posting** — all social posts should be manual and authentic
5. **Don't cross-post identical content** — adapt for each platform
6. **Don't neglect the product** — promotion without substance backfires
