# Bernstein — Content Calendar

**Goal:** establish Bernstein as the de-facto standard for declarative agent orchestration.
**Cadence:** 3 technical posts/month, Twitter threads 2x/week, video quarterly.

---

## Week 1 — Launch burst

| Day | Platform | Content | Status |
|-----|----------|---------|--------|
| D+0 | Twitter | Thread: "3 agents, 47 seconds" — benchmark walkthrough | Draft ready |
| D+1 | Twitter | Thread: "Why deterministic orchestration beats LLM scheduling" | Draft ready |
| D+2 | Blog (Dev.to, Hashnode) | "The Bernstein Architecture: Zero LLM tokens on coordination" | Draft ready |
| D+3 | HN | Show HN post | Draft ready (`docs/launch/hn-post.md`) |
| D+4 | Reddit | r/LocalLLaMA — "I built a multi-agent orchestrator that works with any CLI agent" | Draft ready |
| D+5 | YouTube | 2-minute demo video | Script ready (`docs/assets/youtube-script.md`) |

---

## Month 1 — Technical depth

| Week | Platform | Content |
|------|----------|---------|
| W2 | Blog | "File-based state: why we avoided a database in a multi-agent system" |
| W2 | Twitter | Thread: behind-the-scenes of the janitor verification pass |
| W3 | Blog | "Mixing Claude, Codex, and Gemini in a single pipeline" (adapter architecture) |
| W3 | Twitter | Thread: cost breakdown — heavy model for architecture, cheap for tests |
| W4 | Blog | "30 days of Bernstein self-evolving its own codebase" |
| W4 | Reddit | r/ClaudeAI — thread on Claude Code as an orchestrated agent |

---

## Month 2 — Comparisons and community

| Week | Platform | Content |
|------|----------|---------|
| W5 | Blog | "Bernstein vs. CrewAI: different problems, different tools" (honest comparison) |
| W5 | Twitter | Hot take: "LLM-based scheduling is a trap — here's why" |
| W6 | Blog | "Why we chose git worktrees over Docker for agent isolation" |
| W6 | YouTube | Architecture walkthrough: the orchestrator loop in 10 minutes |
| W7 | Blog | Benchmark deep-dive: methodology, raw data, reproducibility (`docs/blog/multi-agent-benchmark.md`) |
| W7 | Reddit | r/programming — "Building a distributed agent cluster without a message broker" |
| W8 | Blog | "Contributing to Bernstein: how the self-evolution loop works" |

---

## Content pillars

1. **Building in public** — share every benchmark, every decision, every tradeoff
2. **Technical depth** — architecture posts that assume the reader is a senior engineer
3. **Honest comparisons** — no FUD, acknowledge where other tools are better
4. **Benchmarks** — publish raw data and methodology, not just headline numbers
5. **Hot takes** — opinions on where multi-agent AI coding is going

---

## Platform strategy

| Platform | Tone | Frequency | Goal |
|----------|------|-----------|------|
| Twitter/X | Technical, direct, no marketing-speak | 2–3x/week | Reach AI devs |
| HN | Builder story, architecture focus | 1 Show HN at launch, comments on threads | Hacker community |
| Reddit | Honest, answer questions, no spam | 1 post/week across 3 subs | Discovery |
| Dev.to / Hashnode | Full-length technical posts, SEO | 3x/month | Long-tail search |
| YouTube | Demo + architecture walkthroughs | 1x/quarter | Onboarding |

---

## Drafted content

- `docs/social/twitter-thread-47-seconds.md` — launch thread 1
- `docs/social/twitter-thread-deterministic-orchestration.md` — launch thread 2
- `docs/blog/zero-llm-coordination.md` — architecture post
- `docs/social/reddit-local-llama.md` — Reddit launch post
- `docs/launch/hn-post.md` — Show HN
- `docs/assets/youtube-script.md` — 2-minute demo script
- `docs/blog/multi-agent-benchmark.md` — benchmark deep-dive (1.78× faster, 23% cheaper)
- `docs/blog/swe-bench-orchestration-thesis.md` — SWE-Bench results (3-agent Sonnet beats solo Opus)
- `docs/blog/self-evolution-30-days.md` — 30-day self-evolution run on Bernstein's own codebase
