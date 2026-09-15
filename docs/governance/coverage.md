# Governance control coverage

<!-- AUTO-GENERATED from coverage.yaml: run `uv run python scripts/gen_governance_coverage.py --update` to refresh -->

Which failure class each control prevents, where it is enforced, and the test that proves
it. Edit `docs/governance/coverage.yaml`, not this file.

Two things this table is for. The first is the ordinary one: an operator evaluating the
layer for a workload should not have to reconstruct, control by control, which failure each
one answers. The second is the reason it is generated rather than written -- the rows that
say `partial` or `none` are the ones worth reading first, and a table that could only say
`covered` would be a feature list.

**A cited test is one that fails when the control is removed**, not one that merely touches
the module. A test of the happy path proves the code runs, which is a different claim. A row
may not say `covered` without one; `scripts/gen_governance_coverage.py --check` and
`tests/unit/test_governance_coverage_table.py` both refuse it.

**Residual risk is not a caveat.** It is the part of the failure class the control does not
reach, which the operator carries themselves.

10 failure classes: 6 covered, 4 partial, 0 with no control.

| Failure class | Control | Enforced in | Proved by | Residual risk | Status |
| --- | --- | --- | --- | --- | --- |
| Goal hijack through injected content | Prompt-injection scanner over untrusted text, and a typed tool-result boundary so gate output reaches the model as structured results rather than as prose it can be told to reinterpret. | `src/bernstein/core/tokens/prompt_injection.py` | `tests/unit/test_prompt_injection.py::TestIgnorePrevious::test_basic_ignore_previous` | Pattern-based, so it recognises phrasings it has seen. It raises the cost of the obvious attack and is not a decision procedure for the general one. | `partial` |
| Tool misuse and over-broad permissions | Scoped command allowlist: every shell command is checked against the scope the run was granted, and a command outside it is refused rather than logged. | `src/bernstein/core/security/command_allowlist.py` | `tests/unit/test_command_allowlist.py::TestCheckCommand::test_small_scope_denies_rm_rf` | Governs commands, not the arguments a permitted command is given. A permitted tool pointed at the wrong target is a different class, covered by path scoping. | `covered` |
| Identity and privilege abuse across delegation | Delegation scope is recomputable per hop: a child scope that widens any axis its parent imposed fails verification, and the failure names the axis. | `src/bernstein/core/identity/delegation_scope.py` | `tests/unit/identity/test_delegation_narrowing.py::TestWideningIsRejected::test_widening_survives_a_rewritten_and_resealed_chain` | Proves the chain narrows. It does not judge whether the root grant was appropriate to issue in the first place. | `covered` |
| Compromised skills, tools or dependencies | Plugin trust assessment before load: signature fingerprint, declared metadata and provenance decide a trust tier, and an unsigned plugin is reported as unknown rather than trusted by default. | `src/bernstein/plugins/plugin_trust.py` | `tests/unit/test_plugin_trust.py::TestCheckPluginTrust::test_minimal_plugin_returns_unknown` | A signature proves who published a plugin, not that what they published is safe. Nothing here inspects a dependency's transitive tree. | `partial` |
| Unexpected code execution | Sandbox scope enforcement: a relaxation of file-permission scope is admitted only for a scoped mount, so a whole-repo mount keeps enforcement rather than inheriting the relaxation. | `src/bernstein/core/security/guardrails.py` | `tests/unit/test_guardrails_sandbox_scope.py::test_docker_whole_repo_mount_keeps_scope_enforcement` | Bounds what executed code can reach. It does not prevent execution of code an agent was legitimately allowed to run. | `covered` |
| Memory or context poisoning | Hash-chained memory log: every entry carries its own hash and its predecessor's, so edited, inserted, deleted and reordered entries are all detected on verify. | `src/bernstein/core/knowledge/memory_integrity.py` | `tests/unit/test_memory_integrity.py::TestVerifyChain::test_detects_tampered_content` | Detects a chain that was altered after it was written. It does not judge whether a correctly recorded entry was true when recorded. | `covered` |
| Cascading failures across agents | Parallel admission from the code graph: two tasks run concurrently only when both attributions are provable and their symbol neighbourhoods do not intersect. An unprovable attribution serialises rather than proceeding. | `src/bernstein/core/parallel_admission.py` | `tests/unit/test_parallel_admission.py::test_truncated_index_makes_every_attribution_unproven` | Prevents concurrent tasks from colliding in the same code. It is not a fan-out ceiling and not a circuit breaker: a run that spawns without bound is a separate gap. | `partial` ([#5439](https://github.com/sipyourdrink-ltd/bernstein/issues/5439)) |
| Exploitation of human trust in agent output | Absence-coverage classification: a claim that something is absent is UNVERIFIED unless a coverage record shows the ground was actually looked at, so "checked and clean" and "never checked" stop reading alike. | `src/bernstein/core/quality/absence_coverage.py` | `tests/unit/test_completion_absence_coverage.py::test_glob_exists_absence_without_coverage_reads_unverified` | Covers absence claims. A confidently worded positive claim carries no equivalent coverage record. | `partial` ([#5477](https://github.com/sipyourdrink-ltd/bernstein/issues/5477)) |
| Insufficient traceability of decisions | Hash-chained audit log with a canonical byte form, verified independently of the writer, so a single flipped byte or a drifted canonicalisation is detected rather than re-serialised into a chain that still verifies. | `src/bernstein/core/security/audit_chain.py` | `tests/unit/test_audit_chain_byteflip_regression.py::test_canonical_form_drift_is_detected` | Proves the record was not altered after the fact. It cannot prove a decision was recorded at all, which is what governance coverage measures separately. | `covered` |
| Unbounded or runaway behaviour | Spend ledger with a hard cap: once the hard budget is reached the ledger stops admitting calls, independently of the soft warning threshold. | `src/bernstein/core/cost/spend_ledger.py` | `tests/unit/test_spend_ledger.py::TestHardCap::test_hard_halt_blocks_admission` | Bounds spend, which bounds a runaway loop that costs money. A loop that spends nothing -- retrying a local command forever -- is bounded by the stall clock instead, which is a separate control on a separate signal. | `covered` |

## What a status means

* `covered` — a control stands in the way of this failure class, and a test fails when
  it is removed.
* `partial` — a control exists and does not reach the whole class. The linked issue
  names the remainder.
* `none` — no control. The linked issue is the one that would add it.

See [SECURITY.md](../../SECURITY.md) for how to report a gap this table does not list.
