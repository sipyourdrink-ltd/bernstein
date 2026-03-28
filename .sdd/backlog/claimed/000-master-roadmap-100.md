# 000 — Master Roadmap: 100 Features (Research-Backed)

**This is a reference document, not an individual task. Pick items from here to create focused tickets.**

Generated from 76+ demand signals across Reddit, HN, Twitter, GitHub, Gartner, Forrester, Stack Overflow 2025.

---

## P0 — Urgent (10 items, multiple sources confirm)

1. **Structured project memory with AGENTS.md auto-generation** [A] — auto-build project conventions file from codebase analysis
2. **Context compaction before agent spawn** [A] — strip irrelevant files using dependency graph, pass only needed subgraph
3. **Three-tier token budget enforcement** [B] — per-request, per-task, per-run caps with alerts at 50/80/100%
4. **Prompt caching orchestration** [B] — batch repeated system prompts for 90% cached token discount
5. **Mandatory diff review gate** [C] — require human or LLM-judge review before merge
6. **Mutation testing validation** [C] — verify agent tests actually catch bugs, not just coverage
7. **Secrets scanner pre-commit** [D] — scan diffs for API keys, passwords, PII before commit
8. **Network allowlist per agent** [D] — restrict each agent to approved domains only
9. **Trace-level execution timeline** [H] — waterfall view of prompt→tokens→tools→files→tests per task
10. **Self-healing CI with auto-triage** [F] — classify failure type, fix transient, PR for real bugs

## P1 — Critical (24 items)

11. Immutable decision beads (git-backed decision records) [A]
12. Cross-session learning propagation [A]
13. Batch API routing for non-urgent tasks (50% discount) [B]
14. Token-aware task decomposition [B]
15. Agent output code review agent (dedicated QA) [C]
16. Incremental test impact analysis [C]
17. Code churn detector [C]
18. Cryptographic audit log integrity [D]
19. RBAC for agent permissions [D]
20. Container-based agent isolation (gVisor/Firecracker) [D]
21. VS Code sidebar panel (MVP) [E]
22. Neovim floating window integration [E]
23. Team workspace with shared task board [G]
24. Slack/Discord rich notifications (threads, buttons) [G]
25. Grafana dashboard templates [H]
26. Agent reasoning log viewer [H]
27. Real-time cost burn rate display [H]
28. Fan-out/fan-in with synthesis agent [I]
29. Retry with model escalation on failure [I]
30. Task dependency DAG visualization [I]
31. GitHub Actions reusable workflow [F]
32. GitLab CI integration [F]
33. GitHub issue-to-task pipeline [J]
34. Plugin SDK with lifecycle hooks [J]

## P2 — High (30 items)

35. Per-file annotation memory [A]
36. Agent knowledge distillation into skills [A]
37. Codebase convention detector [A]
38. Response caching for deterministic tool calls [B]
39. Cost prediction before task assignment [B]
40. Provider price arbitrage [B]
41. Architecture drift guard [C]
42. Flaky test quarantine and auto-fix [C]
43. PII redaction in agent prompts [D]
44. Prompt injection detection [D]
45. EU AI Act compliance report generator [D]
46. IDE diff preview for agent changes [E]
47. Click-to-assign from IDE [E]
48. Linear/Jira bidirectional sync [G]
49. Multi-human approval chains [G]
50. Shared agent configuration profiles [G]
51. Failure root cause analysis [H]
52. Agent performance comparison dashboard [H]
53. Speculative execution [I]
54. Agent negotiation protocol [I]
55. Warm agent pool (pre-spawn) [I]
56. Docker Compose deployment [F]
57. Helm chart for Kubernetes [F]
58. PR size governor (max 400 lines) [F]
59. PagerDuty/OpsGenie integration [J]
60. Terraform/Pulumi provider [J]
61. Context window right-sizing [B]
62. Cost attribution per team/project [B]
63. Benchmark regression guard [C]
64. Semantic diff review [C]

## P3 — Medium (22 items)

65. Semantic code graph for context routing [A]
66. Memory decay and relevance scoring [A]
67. JetBrains tool window [E]
68. LSP-based agent status indicators [E]
69. Agent work handoff to human [G]
70. Commenting on in-progress agent work [G]
71. Structured log export for SIEM [H]
72. Anomaly detection on agent behavior [H]
73. Priority preemption [I]
74. Natural language orchestration [I]
75. Deployment preview environments [F]
76. Pipeline as code for agent workflows [F]
77. Agent identity management (OAuth tokens) [D]
78. Secure credential injection (Vault) [D]
79. Datadog/New Relic APM integration [J]
80. LLM gateway compatibility (LiteLLM/Portkey) [J]
81. Webhook event system [J]
82. A/B testing for agent strategies [C]
83. Prompt caching analytics [B]
84. Multi-repo knowledge federation [A]
85. Cost attribution dashboard with drill-down [B]
86. Agent cooperation protocol [I]

## P4 — Low (10 items)

87. Cross-repo knowledge federation [A]
88. Idle token burn detection [B]
89. Agent work preview in editor gutter [E]
90. Terminal multiplexer integration [E]
91. Team velocity dashboard [G]
92. Agent utilization heatmap [G]
93. Session replay with step-through [H]
94. Token usage flame graph [H]
95. Multi-language polyglot orchestration [I]
96. Canary deployment for agent changes [F]
97. Dependabot-style maintenance [F]

## P5 — Future (3 items)

98. Voice command interface [E]
99. Browser-based playground [E]
100. Cross-org A2A federation [J]

---

## Priority Distribution

| P0 | P1 | P2 | P3 | P4 | P5 |
|----|----|----|----|----|-----|
| 10 | 24 | 30 | 22 | 10 | 4 |

## Top themes by demand (cross-source frequency)

1. **Context/memory** (7 sources) — agents forget everything between sessions
2. **Cost transparency** (6+ sources) — unpredictable spending kills adoption
3. **Quality gates** (5+ sources) — 1.7x more issues in AI code, verification bottleneck
4. **Security baseline** (5 sources) — 88% orgs report agent security incidents
5. **Multi-provider** (5 sources) — no lock-in, mix models per task
