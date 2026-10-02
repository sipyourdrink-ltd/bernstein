# NIST AI 100-2 crosswalk

Application-security teams threat-model the rest of their ML stack with the
NIST adversarial machine learning taxonomy. This page renders the agent
runtime's guardrails in the identifiers those teams already use, instead of
asking them to learn a bernstein-specific vocabulary.

The mechanisms are the ones shipped today: the guardrail pipeline, the
OWASP ASI detector pack (`docs/security/owasp-asi.md`), the capability
matrix, the sandbox profiles and the audit chain. Nothing here is a
roadmap; a row that says *Partial* says so because a leg of the taxonomy
class is not covered, not because a control is planned.

## Mapped revision

| Field | Value |
|-------|-------|
| Publication | NIST AI 100-2e2025, *Adversarial Machine Learning: A Taxonomy and Terminology of Attacks and Mitigations* |
| Revision | e2025 (the 2025 revision; the earlier NIST AI 100-2 is superseded) |
| Approved by the NIST Editorial Review Board | 2025-03-20 |
| Published | March 2025 |
| DOI | 10.6028/NIST.AI.100-2e2025 |
| Section identifiers below | §-numbers of that revision |

Remapping is due when NIST publishes a later revision of AI 100-2, not when
the detectors change.

## How to read this

Every row names a taxonomy class, the control that answers it, the chain
event the control writes, and a verdict:

- **Covered** - the class is answered by a control that runs on the normal
  path, and the control is the right shape for the attack.
- **Partial** - a control exists and fires, but at least one attacker
  technique in the class is out of its reach. The gap is named in the row.
- **Not covered** / **Not applicable** - no control, with the reason.

The tables are the machine-checked surface of this page: every detector
named in them must exist in the detector registry, every guardrail class in
the pipeline registry, and every chain event in the chain schema
(`src/bernstein/core/security/audit_chain.py`). A rename in code that this
page does not follow fails `tests/unit/test_nist_ai_100_2_crosswalk.py`.

A chain event is named even when the emitter is not yet called from a guard
path - the verdict column says so where that is the case, because "the
schema has a slot for it" is not the same claim as "it is written today".

## Classes that apply to an agent runtime

| NIST AI 100-2 class | Control | Chain event | Verdict |
|---------------------|---------|-------------|---------|
| §3.5 Security of Agents - hijacked agent executes code or reaches the environment through tools | `detect_asi02_tool_misuse`, `detect_asi05_code_execution`, `OwaspAsiGuardrail`, sandbox profiles (`src/bernstein/core/security/sandbox.py`, `src/bernstein/core/security/seccomp_sandbox.py`), `src/bernstein/core/security/sandbox_escape_detector.py`, `src/bernstein/core/security/toolcall_interlock.py` | `sandbox.host_isolation_declared`, `process.reap_receipt`, `toolcall.attestation` | Covered - the tool call is the choke point, and it is gated on both the capability matrix and the sandbox profile before dispatch. All three events are written on the normal path (`host_isolation_declared` and `process.reap_receipt` by the spawner, `toolcall.attestation` by the run-attestation receipt), so the isolation claim and the process teardown are on the chain rather than in a log line |
| §3.5 Security of Agents - agent executes an attacker-specified task instead of the user's | `detect_asi01_goal_hijack`, `PromptInjectionGuardrail` | none from this control - `input.refusal_receipt` is written only by input_refusal.refuse_input (recipes_cmd.py, schedule_supervisor.py) for a malformed recipe-launch or schedule-fire input | Partial - the guardrail reads prompt, retrieved content and tool arguments, and flags the turn; no chain receipt is written for the detection. A hijack phrased as an ordinary instruction that matches no pattern is not blocked here; the §3.5 code-execution row is what bounds the damage |
| §3.4 Indirect Prompt Injection Attacks - resource control injects instructions through data the agent reads | `detect_asi01_goal_hijack` (also scans `retrieved_content`), provenance taint (`src/bernstein/core/security/quarantined_parser.py`) | `provenance.taint_decision`, `provenance.quarantine` | Partial - the taint decision and quarantine receipts exist in the chain schema with an emitter each; neither has a production caller yet, so today the class is answered by the lexical detector and by treating tool output as untrusted at the boundary. This is the largest honest gap on this page |
| §3.4.1 Availability Attacks - time-consuming background tasks, disarming capabilities, disruptive output formatting | `detect_asi08_unbounded_consumption`, `CostGuardrail`, `src/bernstein/core/security/resource_limits.py` | `cost.budget_halt` | Partial - the budget and loop ceilings stop the time-consuming-task technique, and `cost.budget_halt` is written by the spend ledger on the same arithmetic. Disruptive output formatting (homoglyph substitution, empty output) is a downstream-parsing problem this runtime does not police |
| §3.4.2 Integrity Attacks - jailbreak through a resource, execution triggers, knowledge base poisoning, injection hiding, self-propagating injections | `detect_asi01_goal_hijack` on the folded form, `detect_asi06_memory_poisoning`, `src/bernstein/core/security/promptware_detector.py`, `src/bernstein/core/knowledge/memory_integrity.py` | `memory.write` (no production caller yet); `input.refusal_receipt` is not written by the injection detectors, only by input_refusal.refuse_input for malformed recipe-launch and schedule-fire inputs | Partial - injection hiding is the one technique with a real countermeasure: matching runs on text with zero-width codepoints dropped, NFKC applied and confusables mapped to ASCII, so a zero-width space inside a keyword does not hide it. Self-propagating injections are caught only where the outbound send is a gated tool call |
| §3.4.3 Privacy Compromise - the injected instruction makes the system leak restricted resources or user data | `SecretLeakGuardrail`, `src/bernstein/core/security/secrets_broker.py`, `src/bernstein/core/security/url_allowlist.py`, `src/bernstein/core/security/network_policy.py`, `src/bernstein/core/security/socket_guard.py` | `capability.authorization`, `read_set.refusal_receipt` | Partial - secrets are brokered into the call rather than placed in context, and the egress allowlists bound where a leak can go. `capability.authorization` has a schema emitter with no production caller yet; `read_set.refusal_receipt` is the write path that does fire |
| §3.3.1 Direct Prompting Attacks - jailbreaks and prompt manipulation against the assistant itself | `detect_asi01_goal_hijack`, `PromptInjectionGuardrail`, `src/bernstein/core/security/promptware_detector.py` | none from this control - `input.refusal_receipt` is written only by input_refusal.refuse_input (recipes_cmd.py, schedule_supervisor.py) for a malformed recipe-launch or schedule-fire input | Partial - same shape as the indirect case, minus the resource-control leg: pattern-matching detections are evadable by construction, which is why the capability gate rather than the detector carries the weight |
| §3.3.2 Information Extraction - system prompt and context stealing | `SecretLeakGuardrail`, `src/bernstein/core/security/secrets_broker.py`, `src/bernstein/core/security/dlp_scanner.py`, `src/bernstein/core/security/pii_output_gate.py`, `src/bernstein/core/security/redactor.py` | none from this control - `input.refusal_receipt` is written only by input_refusal.refuse_input (recipes_cmd.py, schedule_supervisor.py) for a malformed recipe-launch or schedule-fire input | Partial - the highest-value secret is not in the context window to be extracted, and DLP plus the PII output gate scan the response before it leaves. Extraction of a proprietary *prompt* is not separately detected |
| §3.2 Supply Chain Attacks - §3.2.1 data poisoning and §3.2.2 model poisoning reached through a component the agent loads | `detect_asi04_supply_chain`, `detect_asi07_insecure_a2a`, component signing (`docs/security/mcp-signing.md`), `src/bernstein/core/security/sbom.py`, `src/bernstein/core/security/license_scanner.py`, `src/bernstein/core/security/sigstore_attestation.py` | `skill.install_receipt`, `plugin.install_receipt`, `mcp.capability_drift` | Partial - all three emitters are wired in production (skill catalog, plugin packaging, MCP gateway), and drift after admission is what `mcp.capability_drift` exists to catch. A poisoned upstream model or training corpus is not observable from the client side at all |
| §3.2.1 Data Poisoning Attacks - the corpus the agent retrieves from is poisoned before it is read | `detect_asi06_memory_poisoning`, `src/bernstein/core/knowledge/memory_integrity.py`, `src/bernstein/core/security/quarantined_parser.py` | `memory.write` | Partial - integrity drift in the append-only log is detected; whether a document was true when it was written is not, and cannot be from inside the runtime. The emitter is in the schema with no production caller yet |

## Classes outside this runtime's control surface

| NIST AI 100-2 class | Control | Chain event | Verdict |
|---------------------|---------|-------------|---------|
| §2.2 Evasion Attacks - fool a deployed predictive model at inference time | none | - | Not applicable - evasion presupposes a classifier this runtime owns and serves. It ships no PredAI model; the LLM it calls is the provider's. The closest in-scope attack is hiding an injection from a matcher, which is §3.4.2 above |
| §2.3 Poisoning Attacks - §2.3.1 availability, §2.3.2 targeted, §2.3.3 backdoor, §2.3.4 model poisoning | none | - | Not applicable - every variant modifies training data or parameters. The runtime consumes models; it does not train or fine-tune them |
| §2.4 Privacy Attacks - §2.4.1 data reconstruction, §2.4.2 membership inference, §2.4.3 property inference, §2.4.4 model extraction | none | - | Not applicable - the assets these attacks recover are the provider's training data and weights. The runtime equivalent that *is* in scope, recovering the application's own prompt, is listed under §3.3.2 above |
| §3.3.2 Information Extraction - training data extraction from the model | none | - | Not applicable - the training corpus belongs to the model provider and is not reachable from this runtime |
| §3.1 Attack Classification - stages of learning, attacker goals, capabilities, knowledge | none | - | Not applicable - descriptive axes of the taxonomy rather than attacks; no control has this shape |
| §3.6 Benchmarks for AML Vulnerabilities - JailbreakBench, AgentDojo, Garak, PyRIT and the rest | none | - | Not covered - those benchmarks measure model susceptibility. The eval suite in this repository measures orchestration invariants (merge queue, receipt coverage, gate verdicts), which is a different object |

## Honesty caveats

- A detector that fires is not a proof. Everything in the pipeline is
  heuristic or delegating, and the OWASP ASI page lists the false positives
  that are known.
- The taxonomy itself says full protection against prompt injection is not
  available today, and that designers should assume it is possible when a
  model is exposed to untrusted input. This page follows that stance: the
  verdicts lean on the controls that bound what a successful injection can
  *do* (capability matrix, sandbox, egress allowlists) rather than on the
  controls that try to recognise it.
- Four events exist in the chain schema but have no production caller yet:
  `provenance.taint_decision`, `provenance.quarantine`, `memory.write` and
  `capability.authorization`. They are listed with that stated, so the page does
  not read as coverage the code has not got.

## Related

- Source of the detector pack: `src/bernstein/core/security/owasp_asi_detectors.py`
- Pipeline integration: `src/bernstein/core/security/guardrail_pipeline.py`
- [OWASP ASI01-10 detector pack](owasp-asi.md) - the sibling mapping, against
  the OWASP Top 10 for Agentic Apps
- [Lethal-trifecta security model](lethal-trifecta.md) - the structural
  capability gate that bounds what an injection can reach
- [Capability matrix](capability-matrix.md) - the tool-tag registry
- [MCP server signing + supply-chain scan](mcp-signing.md)
- NIST AI 100-2e2025 - upstream taxonomy this page maps
