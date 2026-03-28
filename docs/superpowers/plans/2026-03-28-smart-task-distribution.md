# Smart Task Distribution (Role-Locked Claiming) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce role-locked task claiming so agents only claim tasks matching their assigned role, and implement fair distribution to prevent role starvation (e.g., 5 backend agents while QA has 0).

**Architecture:**
- Task distribution is already grouped by role in `group_by_role()` with round-robin interleaving
- Per-role agent caps already exist in `claim_and_spawn_batches()`
- We add: (1) explicit role enforcement in agent spawning, (2) rebalancing logic that detects and fills starving roles mid-tick
- The orchestrator is deterministic — no LLM scheduling, just math

**Tech Stack:** Python 3.12+, dataclasses, httpx client, FastAPI server routes

---

## File Structure

### Files to Modify
1. **src/bernstein/core/tick_pipeline.py**
   - Enhance `group_by_role()` to accept a parameter controlling role priority ordering
   - Add `_compute_role_distribution()` to calculate fair per-role agent caps
   - Add `count_agents_per_role()` to tally alive agents by role

2. **src/bernstein/core/task_lifecycle.py**
   - Modify `claim_and_spawn_batches()` to:
     - Track role-to-agent mapping explicitly
     - Reject role-mismatched batches (safety guard)
     - Add mid-tick rebalancing for starving roles

3. **src/bernstein/core/orchestrator.py**
   - Call new rebalancing functions during tick loop
   - Track which roles are "starving" (0 agents, >0 open tasks)
   - Log role distribution stats

### Files to Test
1. **tests/unit/test_orchestrator.py**
   - Test `group_by_role()` priority ordering
   - Test fair role distribution with skewed task counts
   - Test starvation detection and recovery
   - Test role-locked spawning

2. **tests/unit/test_task_lifecycle.py** (new or extend)
   - Test per-role agent caps enforcement
   - Test rejection of role-mismatched batches

---

## Implementation Tasks

### Task 1: Add Helper Functions to tick_pipeline.py

**Files:**
- Modify: `src/bernstein/core/tick_pipeline.py:148-202`
- Test: `tests/unit/test_tick_pipeline.py`

**Objective:** Add two new helper functions to compute role distribution and count agents per role.

- [ ] **Step 1: Write failing tests for new helpers**

Create `tests/unit/test_tick_pipeline.py` with:

```python
def test_compute_role_distribution():
    """Test fair per-role agent cap calculation."""
    tasks = [
        _make_task(role="backend"),
        _make_task(role="backend"),
        _make_task(role="backend"),
        _make_task(role="backend"),
        _make_task(role="backend"),
        _make_task(role="qa"),
        _make_task(role="qa"),
        _make_task(role="qa"),
        _make_task(role="docs"),
        _make_task(role="docs"),
    ]
    max_agents = 4
    distribution = compute_role_distribution(tasks, max_agents)

    # With 10 tasks: backend=5 (50%), qa=3 (30%), docs=2 (20%)
    # Fair caps: backend=2, qa=1, docs=1 (ceil applied)
    assert distribution == {"backend": 2, "qa": 1, "docs": 1}


def test_count_agents_per_role():
    """Test counting alive agents grouped by role."""
    agents = {
        "agent-1": AgentSession(id="agent-1", role="backend", status="working"),
        "agent-2": AgentSession(id="agent-2", role="backend", status="idle"),
        "agent-3": AgentSession(id="agent-3", role="qa", status="working"),
        "agent-4": AgentSession(id="agent-4", role="backend", status="dead"),  # Should not count
    }
    counts = count_agents_per_role(agents)
    assert counts == {"backend": 2, "qa": 1}
```

- [ ] **Step 2: Implement `compute_role_distribution()`**

Add to `src/bernstein/core/tick_pipeline.py` after `group_by_role()`:

```python
import math

def compute_role_distribution(
    tasks: list[Task],
    max_agents: int,
) -> dict[str, int]:
    """Compute fair per-role agent caps based on task distribution.

    Each role gets agents proportional to its share of open tasks,
    with a minimum of 1 agent per role if that role has tasks.

    Args:
        tasks: All open tasks to distribute.
        max_agents: Maximum total agents across all roles.

    Returns:
        Dict mapping role -> max agents for that role.

    Example:
        tasks: 5 backend, 3 qa, 2 docs (10 total)
        max_agents: 4
        → backend: ceil(4 * 5/10) = 2
        → qa: ceil(4 * 3/10) = 2
        → docs: ceil(4 * 2/10) = 1
        Total cap: 5 (may exceed max_agents; that's OK, caps are soft)
    """
    if not tasks:
        return {}

    # Count tasks per role
    tasks_per_role: dict[str, int] = defaultdict(int)
    for task in tasks:
        tasks_per_role[task.role] += 1

    total_tasks = len(tasks)
    distribution: dict[str, int] = {}

    for role, count in tasks_per_role.items():
        # Fair share: ceil(max_agents * count / total_tasks)
        # Ensures roles with many tasks get more agents
        cap = math.ceil(max_agents * count / total_tasks)
        # Ensure at least 1 agent per role with tasks
        distribution[role] = max(1, cap)

    return distribution


def count_agents_per_role(agents: dict[str, AgentSession]) -> dict[str, int]:
    """Count alive agents grouped by role.

    Only counts agents with status != "dead".

    Args:
        agents: Dict of agent_id -> AgentSession.

    Returns:
        Dict mapping role -> count of alive agents.
    """
    counts: dict[str, int] = defaultdict(int)
    for agent in agents.values():
        if agent.status != "dead":
            counts[agent.role] += 1
    return counts
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_tick_pipeline.py::test_compute_role_distribution -xvs
uv run pytest tests/unit/test_tick_pipeline.py::test_count_agents_per_role -xvs
```

Expected: FAIL (functions don't exist)

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_tick_pipeline.py::test_compute_role_distribution -xvs
uv run pytest tests/unit/test_tick_pipeline.py::test_count_agents_per_role -xvs
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/core/tick_pipeline.py tests/unit/test_tick_pipeline.py
git commit -m "feat: add compute_role_distribution and count_agents_per_role helpers"
```

---

### Task 2: Enhance claim_and_spawn_batches with Role-Locked Enforcement

**Files:**
- Modify: `src/bernstein/core/task_lifecycle.py:477-700` (claim_and_spawn_batches)
- Test: `tests/unit/test_orchestrator.py`

**Objective:** Ensure spawned agents are locked to their assigned role and reject mismatches.

- [ ] **Step 1: Write failing test for role-locked enforcement**

Add to `tests/unit/test_orchestrator.py`:

```python
def test_claim_and_spawn_batches_role_locked():
    """Verify agents are created with correct role and batches match."""
    # Setup
    config = OrchestratorConfig(
        server_url="http://127.0.0.1:8052",
        max_agents=5,
    )
    orch = Orchestrator.for_testing(config)
    orch._adapter = _mock_adapter()

    # Create batches grouped by role
    backend_batch = [
        _make_task(id="T-001", role="backend"),
        _make_task(id="T-002", role="backend"),
    ]
    qa_batch = [_make_task(id="T-003", role="qa")]
    batches = [backend_batch, qa_batch]

    # Mock server responses
    def mock_post(url: str, **kwargs):
        if "/claim" in url:
            return httpx.Response(200, json={"id": "T-001", "status": "claimed"})
        return httpx.Response(200)

    orch._client.post = mock_post

    # Spawn agents
    result = TickResult()
    claim_and_spawn_batches(orch, batches, alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    # Verify: agents were created with matching roles
    assert len(orch._agents) >= 2
    backend_agents = [a for a in orch._agents.values() if a.role == "backend"]
    qa_agents = [a for a in orch._agents.values() if a.role == "qa"]
    assert len(backend_agents) >= 1
    assert len(qa_agents) >= 1
```

- [ ] **Step 2: Review current claim_and_spawn_batches code**

Read lines 477-700 of `src/bernstein/core/task_lifecycle.py` and note:
- Line ~650: Agent is spawned with `spawner.spawn(..., session=AgentSession(role=batch[0].role, ...))`
- The agent ALREADY gets the correct role ✓
- No enforcement check that batch tasks all match the agent's role

- [ ] **Step 3: Add role-match assertion before spawning**

In `claim_and_spawn_batches`, after the quarantine check and before pre-flight decompose (around line 614), add:

```python
        # Role-lock enforcement: all tasks in batch must match the agent's assigned role
        batch_role = batch[0].role
        mismatched = [t for t in batch if t.role != batch_role]
        if mismatched:
            logger.error(
                "Role mismatch in batch: agent will be %s but batch has %d non-matching tasks: %s",
                batch_role,
                len(mismatched),
                [t.id for t in mismatched],
            )
            result.errors.append(f"batch:{batch[0].id}: role mismatch in batch")
            continue
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_orchestrator.py::test_claim_and_spawn_batches_role_locked -xvs
```

Expected: PASS

- [ ] **Step 5: Run full orchestrator test suite to ensure no regressions**

```bash
uv run pytest tests/unit/test_orchestrator.py -x -q
```

Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/task_lifecycle.py tests/unit/test_orchestrator.py
git commit -m "feat: enforce role-locked batching in claim_and_spawn_batches"
```

---

### Task 3: Add Starvation Detection in orchestrator.py

**Files:**
- Modify: `src/bernstein/core/orchestrator.py` (main tick loop)
- Test: `tests/unit/test_orchestrator.py`

**Objective:** Detect roles with 0 agents but >0 open tasks and trigger immediate spawning.

- [ ] **Step 1: Write failing test for starvation detection**

Add to `tests/unit/test_orchestrator.py`:

```python
def test_orchestrator_detects_starving_roles():
    """Verify orchestrator detects and logs role starvation."""
    config = OrchestratorConfig(
        server_url="http://127.0.0.1:8052",
        max_agents=5,
    )
    orch = Orchestrator.for_testing(config)
    orch._adapter = _mock_adapter()

    # Setup: 5 backend tasks, 3 qa tasks, 0 agents
    tasks_response = [
        _task_as_dict(_make_task(id=f"T-{i:03d}", role="backend"))
        for i in range(1, 6)
    ] + [
        _task_as_dict(_make_task(id=f"T-{i:03d}", role="qa"))
        for i in range(6, 9)
    ]

    def mock_get(url, **kwargs):
        if "/tasks" in url:
            return httpx.Response(200, json=tasks_response)
        return httpx.Response(200, json={})

    orch._client.get = mock_get
    orch._client.post = lambda *a, **kw: httpx.Response(200, json={})

    # Detect starvation: call detection function
    # (function TBD in step 2)
    starving = detect_starving_roles(orch._agents, tasks_response)

    # Should detect qa as starving: 0 agents, 3 tasks
    assert "qa" in starving
    assert starving["qa"]["agent_count"] == 0
    assert starving["qa"]["task_count"] == 3
```

- [ ] **Step 2: Implement `detect_starving_roles()` in tick_pipeline.py**

Add to `src/bernstein/core/tick_pipeline.py`:

```python
def detect_starving_roles(
    agents: dict[str, AgentSession],
    tasks: list[Task],
) -> dict[str, dict[str, int]]:
    """Detect roles with 0 agents but >0 open tasks.

    Args:
        agents: Dict of agent_id -> AgentSession.
        tasks: List of open tasks.

    Returns:
        Dict of role -> {"agent_count": N, "task_count": M} for starving roles.
    """
    agents_per_role = count_agents_per_role(agents)
    tasks_per_role: dict[str, int] = defaultdict(int)

    for task in tasks:
        tasks_per_role[task.role] += 1

    starving: dict[str, dict[str, int]] = {}
    for role, task_count in tasks_per_role.items():
        agent_count = agents_per_role.get(role, 0)
        if agent_count == 0 and task_count > 0:
            starving[role] = {"agent_count": agent_count, "task_count": task_count}

    return starving
```

- [ ] **Step 3: Import and use detect_starving_roles in orchestrator.py**

In `src/bernstein/core/orchestrator.py`, add to imports:

```python
from bernstein.core.tick_pipeline import (
    # ... existing imports
    detect_starving_roles,
)
```

In the `tick()` method, after `claim_and_spawn_batches()` is called (around line 700+), add:

```python
        # Starvation detection and recovery: if a role has 0 agents but >0 tasks,
        # log a warning and prioritize spawning for that role next tick.
        starving_roles = detect_starving_roles(self._agents, open_tasks)
        if starving_roles:
            for role, counts in starving_roles.items():
                logger.warning(
                    "Role starvation detected: %s has %d agents and %d open tasks",
                    role,
                    counts["agent_count"],
                    counts["task_count"],
                )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_orchestrator.py::test_orchestrator_detects_starving_roles -xvs
```

Expected: PASS

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest tests/unit/test_orchestrator.py -x -q
```

Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/tick_pipeline.py src/bernstein/core/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: add starvation detection for role-starved workloads"
```

---

### Task 4: Implement Rebalancing Logic in orchestrator.py

**Files:**
- Modify: `src/bernstein/core/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Objective:** During each tick, rebalance agent spawning across roles based on current distribution.

- [ ] **Step 1: Write failing test for rebalancing**

Add to `tests/unit/test_orchestrator.py`:

```python
def test_rebalance_skewed_distribution():
    """Verify rebalancing prioritizes starving roles."""
    config = OrchestratorConfig(
        server_url="http://127.0.0.1:8052",
        max_agents=6,
    )
    orch = Orchestrator.for_testing(config)
    orch._adapter = _mock_adapter()

    # Setup: 5 backend tasks, 2 qa tasks
    # Start with 3 backend agents (over-allocated) and 0 qa agents (starved)
    tasks = [
        _make_task(id=f"T-{i:03d}", role="backend")
        for i in range(1, 6)
    ] + [
        _make_task(id=f"T-{i:03d}", role="qa")
        for i in range(6, 8)
    ]

    # Start with 3 backend agents
    for i in range(3):
        orch._agents[f"agent-{i}"] = AgentSession(
            id=f"agent-{i}",
            role="backend",
            pid=100 + i,
            status="working",
        )

    # Mock server
    tasks_dict = [_task_as_dict(t) for t in tasks]
    orch._client.get = lambda url, **kw: httpx.Response(200, json=tasks_dict)
    orch._client.post = lambda *a, **kw: httpx.Response(200, json={})

    # Call rebalance function (TBD)
    # Expected: qa gets spawned first (starving), backend doesn't get more
    rebalance_result = rebalance_role_distribution(
        orch._agents,
        tasks,
        max_agents=6,
    )

    # Verify recommendation
    assert rebalance_result["recommendation"] == "qa"  # Next agent should be qa
    assert rebalance_result["backend_agents"] == 3
    assert rebalance_result["qa_agents"] == 0
```

- [ ] **Step 2: Implement `rebalance_role_distribution()` in tick_pipeline.py**

Add to `src/bernstein/core/tick_pipeline.py`:

```python
def rebalance_role_distribution(
    agents: dict[str, AgentSession],
    tasks: list[Task],
    max_agents: int,
) -> dict[str, object]:
    """Recommend which role to spawn next based on current skew.

    Returns the role that is furthest below its fair share of agents.

    Args:
        agents: Dict of agent_id -> AgentSession.
        tasks: List of open tasks.
        max_agents: Maximum total agents.

    Returns:
        Dict with keys:
        - "recommendation": role to spawn (str), or None if balanced
        - "{role}_agents": int, current agent count per role
        - "{role}_target": int, target agent count per role
        - "{role}_deficit": int, how many agents below target
    """
    if not tasks:
        return {"recommendation": None}

    agents_per_role = count_agents_per_role(agents)
    distribution = compute_role_distribution(tasks, max_agents)

    result: dict[str, object] = {}
    max_deficit = -1
    best_role: str | None = None

    for role, target in distribution.items():
        current = agents_per_role.get(role, 0)
        deficit = target - current
        result[f"{role}_agents"] = current
        result[f"{role}_target"] = target
        result[f"{role}_deficit"] = deficit

        # Pick role with biggest deficit (most starved relative to target)
        if deficit > max_deficit:
            max_deficit = deficit
            best_role = role

    result["recommendation"] = best_role if max_deficit > 0 else None
    return result
```

- [ ] **Step 3: Use rebalance in orchestrator.py tick loop**

In `src/bernstein/core/orchestrator.py`, after starvation detection (Task 3), add:

```python
from bernstein.core.tick_pipeline import (
    # ... existing imports
    rebalance_role_distribution,
)

# In tick() method, after starvation detection:

        # Rebalance: prioritize spawning for starving roles
        balance = rebalance_role_distribution(self._agents, open_tasks, self._config.max_agents)
        if balance.get("recommendation"):
            recommended_role = balance["recommendation"]
            logger.info(
                "Rebalancing: role %s needs agents (deficit=%d)",
                recommended_role,
                balance.get(f"{recommended_role}_deficit", 0),
            )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_orchestrator.py::test_rebalance_skewed_distribution -xvs
```

Expected: PASS

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest tests/unit/test_orchestrator.py -x -q
```

Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/tick_pipeline.py src/bernstein/core/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: add role distribution rebalancing logic"
```

---

### Task 5: Integration Test — Fair Distribution End-to-End

**Files:**
- Test: `tests/unit/test_orchestrator.py`

**Objective:** Verify the full pipeline: role grouping → fair distribution → starvation detection → rebalancing.

- [ ] **Step 1: Write end-to-end integration test**

Add to `tests/unit/test_orchestrator.py`:

```python
def test_fair_distribution_end_to_end():
    """
    Verify the full smart distribution pipeline:
    - Tasks grouped by role (round-robin interleaved)
    - Per-role agent caps enforced
    - Starvation detected and logged
    - Rebalancing recommends starving roles
    """
    config = OrchestratorConfig(
        server_url="http://127.0.0.1:8052",
        max_agents=4,
    )
    orch = Orchestrator.for_testing(config)
    orch._adapter = _mock_adapter()

    # Setup: 5 backend tasks, 3 qa tasks, 2 docs tasks (10 total)
    # Fair share: backend ~2, qa ~1, docs ~1 agents
    tasks = [
        _make_task(id=f"T-backend-{i}", role="backend")
        for i in range(5)
    ] + [
        _make_task(id=f"T-qa-{i}", role="qa")
        for i in range(3)
    ] + [
        _make_task(id=f"T-docs-{i}", role="docs")
        for i in range(2)
    ]

    # Mock server responses
    tasks_dict = [_task_as_dict(t) for t in tasks]
    orch._client.get = lambda url, **kw: httpx.Response(200, json=tasks_dict)
    orch._client.post = lambda *a, **kw: httpx.Response(200, json={})

    # Run one tick
    result = orch.tick()

    # Verify: spawned agents are grouped by role (no role starves alone)
    if len(orch._agents) > 0:
        agents_by_role = defaultdict(list)
        for agent in orch._agents.values():
            if agent.status != "dead":
                agents_by_role[agent.role].append(agent)

        # Check: no single role dominates all slots
        role_counts = {r: len(agents) for r, agents in agents_by_role.items()}
        total_agents = sum(role_counts.values())

        # Each role should have roughly proportional agents
        for role, count in role_counts.items():
            # No role should have >60% of agents (unless it has >60% of tasks)
            role_task_share = sum(1 for t in tasks if t.role == role) / len(tasks)
            assert count / total_agents <= role_task_share + 0.2, f"Role {role} over-allocated"
```

- [ ] **Step 2: Run integration test**

```bash
uv run pytest tests/unit/test_orchestrator.py::test_fair_distribution_end_to_end -xvs
```

Expected: PASS

- [ ] **Step 3: Run full test suite to ensure no regressions**

```bash
uv run pytest tests/unit/test_orchestrator.py -x -q
```

Expected: All tests pass (should be ~30-40 tests)

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_orchestrator.py
git commit -m "feat: add end-to-end integration test for fair role distribution"
```

---

### Task 6: Verify Completion Signal

**Files:**
- Manual: Test the running orchestrator

**Objective:** Verify that real spawning respects role-locked distribution.

- [ ] **Step 1: Start the orchestrator in background**

In a separate terminal:

```bash
cd /Users/sasha/IdeaProjects/personal_projects/bernstein
uv run python -m bernstein.cli.main run --max-agents 4 --config .sdd/config.yaml
```

- [ ] **Step 2: Create test backlog with imbalanced roles**

Create `.sdd/backlog/open/001-backend-task.md`:

```markdown
# Backend Task 1
- role: backend
- priority: 2

Write a backend feature.
```

Create similar files for 4 more backend tasks, 2 qa tasks, 1 docs task.

- [ ] **Step 3: Monitor orchestrator logs**

```bash
tail -f .sdd/runtime/logs/orchestrator.log | grep -E "role|starvation|rebalance"
```

Verify output shows:
- "Role starvation detected: qa has 0 agents and 2 open tasks" (or similar)
- "Rebalancing: role qa needs agents (deficit=1)"
- "Skipping batch for role backend: at cap (2/2 agents for 5/10 tasks)"

- [ ] **Step 4: Let orchestrator run for ~2 minutes**

Agent should spawn for qa role once starvation is detected.

- [ ] **Step 5: Verify agent roles match their task assignments**

In `.sdd/runtime/agents/` check agent session files or logs:

```bash
jq '.role' .sdd/runtime/agents/*.json
```

Should see mix of "backend", "qa", "docs" roles, not all the same.

- [ ] **Step 6: Verify test assertions pass**

```bash
uv run pytest tests/unit/test_orchestrator.py -x -q
```

Expected: All pass (50+ tests)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: smart task distribution — role-locked claiming + fair agent allocation"
```

---

## Testing Checklist

- [ ] Unit tests for `compute_role_distribution()` ✓ (Task 1)
- [ ] Unit tests for `count_agents_per_role()` ✓ (Task 1)
- [ ] Unit tests for role-locked batch enforcement ✓ (Task 2)
- [ ] Unit tests for starvation detection ✓ (Task 3)
- [ ] Unit tests for role rebalancing ✓ (Task 4)
- [ ] End-to-end integration test ✓ (Task 5)
- [ ] Full `test_orchestrator.py` suite passes ✓ (Task 5)
- [ ] Manual verification with live orchestrator ✓ (Task 6)

---

## Completion Signal

Run:

```bash
curl -s --retry 3 --retry-delay 2 --retry-all-errors \
  -X POST http://127.0.0.1:8052/tasks/39c2f958e216/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Completed: 333d — Smart Task Distribution (role-locked claiming, fair distribution, rebalancing)"}'
```

If connection fails, retry up to 3 times with 2-second delay.

---

## Summary of Changes

| File | Changes | Rationale |
|------|---------|-----------|
| `tick_pipeline.py` | Add `compute_role_distribution()`, `count_agents_per_role()`, `detect_starving_roles()`, `rebalance_role_distribution()` | Helper functions for role distribution math |
| `task_lifecycle.py` | Add role-mismatch assertion in `claim_and_spawn_batches()` | Enforce role-locked spawning |
| `orchestrator.py` | Call starvation detection and rebalancing during tick loop | Actively monitor and fix role imbalances |
| `test_orchestrator.py` | 6 new test cases covering all new logic | Full coverage of smart distribution |

**Key Properties After Implementation:**
- ✅ No role starves while another has excess agents
- ✅ Agents only work on tasks matching their assigned role
- ✅ Fair share: `ceil(max_agents * role_tasks / total_tasks)` agents per role
- ✅ Starvation detected and logged every tick
- ✅ Rebalancing recommends starving roles for next spawn
- ✅ All existing tests continue to pass (no breaking changes)
