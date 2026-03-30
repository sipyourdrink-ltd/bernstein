# F96 — Formal Verification Gateway

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Standard test-based verification cannot guarantee correctness for safety-critical outputs; formal mathematical proofs are needed but have no integration point in the pipeline.

## Solution
- Add a formal verification gateway that agent outputs pass through before merge
- Support proof checkers: Z3 (SMT solver) and Lean4 (interactive theorem prover)
- Define verifiable properties in `bernstein.yaml` under a `formal_verification` section (e.g., `invariant: "output.length > 0"`, `property: "no_null_dereference"`)
- Gateway translates properties into proof obligations and submits to the configured checker
- If any property is violated, task fails with a detailed counterexample from the solver
- Support both automatic (Z3) and semi-automatic (Lean4 with pre-written lemmas) verification modes
- Add `bernstein verify --formal <task-id>` for manual invocation

## Acceptance
- [ ] Formal verification gateway integrated into task completion pipeline
- [ ] Z3 SMT solver integration functional for automatic property checking
- [ ] Lean4 integration functional for semi-automatic verification with lemmas
- [ ] Properties defined in `bernstein.yaml` `formal_verification` section
- [ ] Task fails with counterexample when property is violated
- [ ] `bernstein verify --formal <task-id>` runs verification manually
- [ ] Gateway is optional and skipped when no properties are defined
