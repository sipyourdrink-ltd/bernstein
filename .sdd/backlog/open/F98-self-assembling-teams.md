# F98 — Self-Assembling Agent Teams

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Users must manually select and configure agent teams for each task, which requires deep knowledge of available agents and their capabilities, slowing down complex multi-skill workflows.

## Solution
- Implement self-assembling agent teams: given a high-level goal, agents autonomously recruit other agents based on required skills
- Define a skill taxonomy in the agent catalog: each agent declares skills (e.g., `python`, `testing`, `security-audit`, `documentation`)
- Goal analyzer decomposes the user's goal into required skill sets
- Recruitment algorithm matches required skills to available agents, assembling the minimal team
- Agents can propose additional recruits if they identify skill gaps during execution
- Team formation logged and displayed: "Assembled team: Agent-A (python, testing), Agent-B (security-audit)"
- Add `bernstein run --auto-team -g "goal description"` to trigger self-assembly

## Acceptance
- [ ] Skill taxonomy defined in agent catalog with per-agent skill declarations
- [ ] Goal analyzer decomposes goals into required skill sets
- [ ] Recruitment algorithm assembles minimal agent team matching required skills
- [ ] Agents can propose additional recruits during execution for skill gaps
- [ ] Team formation displayed in run output with agent-skill mapping
- [ ] `bernstein run --auto-team -g "goal"` triggers self-assembling team
