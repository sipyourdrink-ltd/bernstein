# COBOL Modernization with Parallel Agents

> 800 billion lines of COBOL run the world's banks, insurers, and governments. Migrating them is a decade-long slog — unless you parallelize.

---

## The problem

COBOL modernization projects fail for two reasons: they take too long and they break business logic. A typical bank has 5,000–50,000 COBOL programs. Translating one program at a time — even with AI — takes months. Sequential work means sequential risk: one bad translation blocks everything downstream.

The industry's answer so far: IBM's watsonx Code Assistant for Z (proprietary, $$$), Anthropic's Code Modernization Playbook (single-agent, Claude Code), and Azure's Legacy-Modernization-Agents (demo-quality). All single-threaded. All leave the hard part — parallel execution, verification, isolation — as an exercise for the reader.

## Why multi-agent orchestration fits

COBOL programs are naturally modular. A batch settlement system might have 200 programs that CALL each other but can be translated independently once you map the interfaces. This is embarrassingly parallel work:

```
Program A (accounts)  →  Agent 1  →  Java class A  →  equivalence test
Program B (ledger)    →  Agent 2  →  Java class B  →  equivalence test
Program C (reports)   →  Agent 3  →  Java class C  →  equivalence test
...
```

Each agent works in its own git worktree. No merge conflicts. No shared state. The janitor verifies that each translation compiles, passes equivalence tests, and doesn't regress existing code.

## The plan file

Bernstein encodes this as a four-stage YAML plan:

```yaml
stages:
  - name: "Discovery"        # scan COBOL, map dependencies
  - name: "Specification"    # formal specs + test specs per module
    depends_on: ["Discovery"]
  - name: "Translation"      # parallel agents, one per module group
    depends_on: ["Specification"]
  - name: "Verification"     # equivalence tests, static analysis
    depends_on: ["Translation"]
```

The first two stages run sequentially — you need to understand the codebase before translating. But Translation fans out to 8 agents working simultaneously, each converting a module group. Verification runs janitor checks: `mvn test`, `mvn verify`, SpotBugs, PMD.

Full plan: [`examples/plans/cobol-modernization.yaml`](../../examples/plans/cobol-modernization.yaml)

Run it:

```bash
bernstein run examples/plans/cobol-modernization.yaml
```

## What makes this different

**Deterministic orchestration.** The scheduler is Python code, not an LLM. No tokens spent deciding which agent gets which task. No hallucinated task assignments. The plan says "translate core modules" and "translate data access" — both run in parallel because they don't depend on each other.

**Git worktree isolation.** Agent 1 translating the accounts module can't accidentally break Agent 2's work on the ledger module. Each agent gets a fresh worktree. The janitor merges only what passes.

**Equivalence verification.** The Specification stage generates test specs — input/output pairs extracted from COBOL business rules. The Verification stage implements them as JUnit tests. If `COMPUTE TOTAL-AMT = SUBTOTAL * 1.08` in COBOL doesn't produce the same result as `totalAmt = subtotal.multiply(new BigDecimal("1.08"))` in Java, the test fails and the task gets re-queued.

**Model mixing.** Use Claude for complex business logic translation, Gemini for boilerplate data access code, Codex for test generation. Each agent picks the best model for the job. The orchestrator doesn't care — it just needs `mvn compile` to pass.

## Numbers

From our internal benchmarks on a 47-hour sprint (different codebase, same orchestrator):

- 12 agents in parallel
- 737 tasks completed
- 15.7 tasks/hour throughput
- 1.78x faster than single-agent sequential

Applied to COBOL: a 500-program migration that takes one agent 3 months could finish in ~6 weeks with 8 agents, assuming similar task independence. Actual speedup depends on the dependency graph — tightly coupled programs serialize, independent ones parallelize.

## Getting started

```bash
pip install bernstein

# Point it at your COBOL codebase
cd your-cobol-project
bernstein init

# Copy and customize the plan
cp $(python -c "import bernstein; print(bernstein.__path__[0])")/../../examples/plans/cobol-modernization.yaml plan.yaml
# Edit plan.yaml: set source_dir, adjust budget, pick agents

# Run
bernstein run plan.yaml
```

The plan is a starting point. Real COBOL codebases have CICS, DB2, IMS, MQ — adjust stages and steps to match your stack. The verification stage is non-negotiable: never merge translated code without equivalence tests.

---

*Bernstein is Apache 2.0 licensed. The COBOL modernization plan template ships with every install.*
