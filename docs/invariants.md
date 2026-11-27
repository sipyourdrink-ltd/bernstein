# Enforced invariants

Each entry below states one falsifiable property, the code path that enforces it, the test node IDs that fail if the property is broken, and the known limits of that check.

## 1. Budget cap enforcement

**Invariant.** When spend reaches `100%` of a positive `budget_usd` cap under `BudgetPolicy.default()` (`BudgetAction.ABORT`, with `CostTracker.status().should_stop` set to `True`), `Orchestrator.tick()` spawns zero new agents; when `Orchestrator._enforce_budget_killswitch()` runs while `CostTracker.status().should_stop` is `True`, it sends `SHUTDOWN` to live sessions and kills any still running after `kill_grace_period_s`; and `route_task()` with `budget_aware_routing_enabled=True` suppresses automatic escalation when remaining budget falls strictly below `BUDGET_AWARE_OPUS_MARGIN * estimated_opus_cost_usd` (`DEFAULT_OPUS_TASK_COST_USD` by default).

**Enforced in**

- `src/bernstein/core/cost/cost_tracker.py` (`CostTracker.record`, `CostTracker.status`)
- `src/bernstein/core/cost/budget_actions.py` (`BudgetPolicy.default`, `BudgetPolicy.evaluate`, `BudgetAction`)
- `src/bernstein/core/orchestration/orchestrator.py` (`Orchestrator.tick`, `Orchestrator._evaluate_budget_policy`, `Orchestrator._enforce_budget_killswitch`)
- `src/bernstein/core/routing/router_core.py` (`_check_opus_override`, `route_task`, `set_budget_context`)

**Tests**

- `tests/unit/test_regression_orchestrator.py::TestCostTrackerBudgetEnforcement::test_stops_at_100_percent`
- `tests/unit/test_regression_orchestrator.py::TestCostTrackerBudgetEnforcement::test_budget_blocks_spawn_in_tick`
- `tests/unit/test_budget_killswitch.py::TestBudgetKillSwitchTransition::test_shutdown_signal_sent_to_all_live_agents`
- `tests/unit/test_budget_killswitch.py::TestBudgetKillSwitchGracePeriod::test_sigkill_after_grace_window`
- `tests/unit/test_budget_killswitch.py::TestBudgetKillSwitchGracePeriod::test_sigkill_only_once_per_agent`
- `tests/unit/test_budget_killswitch.py::TestBudgetKillSwitchHeartbeatReap::test_sigkill_reaps_heartbeat_loop_per_agent`
- `tests/unit/test_budget_aware_routing.py::TestCheckOpusOverrideBudgetAware::test_near_cap_priority_1_architect_downgrades`
- `tests/unit/test_budget_aware_routing.py::TestCheckOpusOverrideBudgetAware::test_threshold_just_below_margin_downgrades`
- `tests/unit/test_budget_aware_routing.py::TestRouteTaskBudgetAware::test_route_task_downgrades_near_cap`

**Known limits**

- Setting `budget_usd=0.0` with `hard_budget_usd=0.0` (the default) disables the stop condition (`should_stop` remains `False` and `remaining_usd` is `inf`): `tests/unit/test_regression_orchestrator.py::TestCostTrackerBudgetEnforcement::test_unlimited_budget_never_stops`. When `budget_usd=0.0` and a positive `hard_budget_usd` is reached, `CostTracker.status().should_stop` flips to `True`, while `Orchestrator._evaluate_budget_policy()` still returns `None`.
- `Orchestrator._tick_internal()` gates spawns and calls `_enforce_budget_killswitch()` on `BudgetAction.ABORT` from `Orchestrator._evaluate_budget_policy()`, not directly on `CostTracker.status().should_stop`. If `should_stop` flips to `True` below `100%` (for example via `hard_stop_threshold < 1.0` or `hard_budget_usd < budget_usd`), if a custom `BudgetPolicy` omits or raises the `1.0` `ABORT` threshold, or if an earlier `cost_dispatch_halt` branch holds the tick, the `ABORT` branch and `_enforce_budget_killswitch()` do not run on that tick.
- The named tests in `tests/unit/test_budget_killswitch.py` bind `Orchestrator._enforce_budget_killswitch` onto a `SimpleNamespace` stub and call it directly rather than exercising it through `Orchestrator.tick()`.
- In-flight sessions are not killed before `kill_grace_period_s` elapses, so spend from running tasks can continue during the grace window: `tests/unit/test_budget_killswitch.py::TestBudgetKillSwitchGracePeriod::test_no_sigkill_during_grace_window`.
- Raising `budget_usd` so `should_stop` returns to `False` re-arms the kill-switch and clears its fired state: `tests/unit/test_budget_killswitch.py::TestBudgetKillSwitchRearm::test_rearms_when_spend_drops_back`.
- Budget-aware routing does not downgrade when `budget_aware_routing_enabled=False`, when remaining budget is `None` or `inf`, when remaining budget equals the exact margin boundary, or when `task.model` is set explicitly: `tests/unit/test_budget_aware_routing.py::TestCheckOpusOverrideBudgetAware::test_flag_disabled_preserves_legacy_behavior`, `tests/unit/test_budget_aware_routing.py::TestCheckOpusOverrideBudgetAware::test_unknown_budget_preserves_legacy_behavior`, `tests/unit/test_budget_aware_routing.py::TestCheckOpusOverrideBudgetAware::test_infinite_budget_preserves_legacy_behavior`, `tests/unit/test_budget_aware_routing.py::TestCheckOpusOverrideBudgetAware::test_threshold_boundary_exactly_at_margin`, `tests/unit/test_budget_aware_routing.py::TestRouteTaskBudgetAware::test_manager_override_not_downgraded`.

## 2. Explicit tool allowlist denies unlisted tools

**Invariant.** When an explicit `list[str]` allowlist is passed to `check_tool_allowed(tool_name, allowlist)` (including the empty list `[]`), every tool name not matching an entry in the list verbatim returns `False`.

**Enforced in**

- `src/bernstein/core/agents/spawner_warm_pool.py` (`check_tool_allowed`, `build_tool_allowlist_env`, `parse_tool_allowlist_env`; re-exported by `src/bernstein/core/agents/spawner.py`)

**Tests**

- `tests/unit/test_worker_tool_allowlist.py::TestCheckToolAllowed::test_empty_allowlist_denies_all`
- `tests/unit/test_worker_tool_allowlist.py::TestCheckToolAllowed::test_denies_when_not_in_allowlist`
- `tests/unit/test_worker_tool_allowlist.py::TestCheckToolAllowed::test_case_sensitive`

**Known limits**

- Passing `allowlist=None` allows every tool: `tests/unit/test_worker_tool_allowlist.py::TestCheckToolAllowed::test_allows_when_no_allowlist`.
- Passing `[]` to `build_tool_allowlist_env([])` returns `{}` (omitting `BERNSTEIN_TOOL_ALLOWLIST`), and `parse_tool_allowlist_env()` returns `None` when `BERNSTEIN_TOOL_ALLOWLIST` is unset or empty: `tests/unit/test_worker_tool_allowlist.py::TestBuildToolAllowlistEnv::test_empty_list`, `tests/unit/test_worker_tool_allowlist.py::TestParseToolAllowlistEnv::test_returns_none_when_empty`, `tests/unit/test_worker_tool_allowlist.py::TestParseToolAllowlistEnv::test_returns_none_when_not_set`.
- These tests cover the helper functions in isolation; workers or adapters that do not read `BERNSTEIN_TOOL_ALLOWLIST` or call `check_tool_allowed()` are not constrained by this check.

## 3. Scope guardrail blocks out-of-scope files and traversal

**Invariant.** When both `scope` and `modified_files` are non-empty in the context passed to `ScopeGuardrail.check_output()`, any path outside the allowed relative path-segment prefixes — including sibling-prefix directories, `..` traversal with forward slashes or backslashes, absolute paths, and contexts whose scope entries are not valid relative paths — returns `passed=False`.

**Enforced in**

- `src/bernstein/core/security/guardrail_pipeline.py` (`ScopeGuardrail.check_output`, `_path_segments`, `_within_scope`, `GuardrailPipeline.check_output`)
- `src/bernstein/core/security/path_containment.py` (`validate_relative_path`)

**Tests**

- `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_blocks_out_of_scope_files`
- `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_blocks_sibling_directory_sharing_a_prefix`
- `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_blocks_traversal_out_of_scope`
- `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_blocks_absolute_path`
- `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_blocks_backslash_traversal_on_every_platform`
- `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_scope_entry_that_is_not_relative_admits_nothing`

**Known limits**

- When `scope` is missing or empty, or `modified_files` is missing or empty, `ScopeGuardrail.check_output()` returns `passed=True`: `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_passes_when_no_scope`, `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_passes_when_no_modified_files`.
- `ScopeGuardrail.check_input()` returns `passed=True` on every input, so callers invoking only `GuardrailPipeline.check_input()` do not evaluate scope: `tests/unit/test_guardrail_pipeline.py::TestScopeGuardrail::test_input_always_passes`.
- Evaluation inspects the string paths in `context["modified_files"]` lexically; it does not inspect the filesystem or resolve symbolic links, and applies only on code paths that invoke `ScopeGuardrail.check_output()` or `GuardrailPipeline.check_output()`.

## 4. External policy hook fails closed on error by default

**Invariant.** Under the default `PolicyHookRegistry(fail_open=False)` mode, a hook that raises an exception or whose policy engine binary is missing evaluates to `HookVerdict.UNAVAILABLE` (not `HookVerdict.ABSTAIN`), and `PolicyHookRegistry.first_decisive()` returns `HookVerdict.DENY` when `UNAVAILABLE` is reached before any earlier `ALLOW`; in `CedarHook`, for matching `action == "<name>"` rules, `forbid` overrides `permit`, and statements containing the substrings `when`, `unless`, or `?principal` raise `ValueError` at construction.

**Enforced in**

- `src/bernstein/core/security/external_policy_hook.py` (`PolicyHookRegistry.__init__`, `PolicyHookRegistry.evaluate`, `PolicyHookRegistry.first_decisive`, `OPAHook.evaluate`, `CedarHook.__init__`, `CedarHook.evaluate`)
- `src/bernstein/core/agents/spawner_core.py` (`AgentSpawner._enforce_external_admission`, when `policy_hook_registry` is passed to `AgentSpawner`)

**Tests**

- `tests/unit/test_external_policy_hook.py::TestPolicyHookRegistry::test_fail_closed_on_error`
- `tests/unit/test_external_policy_hook.py::TestOPAHook::test_opa_binary_not_found_is_unavailable_not_abstain`
- `tests/unit/test_external_policy_hook.py::TestCedarHook::test_deny_takes_priority`
- `tests/unit/test_external_policy_hook.py::TestCedarHook::test_forbid_unsupported_when_refused`
- `tests/unit/test_external_policy_hook.py::TestCedarHook::test_forbid_unsupported_unless_refused`
- `tests/unit/test_external_policy_hook.py::TestCedarHook::test_forbid_unsupported_principal_refused`
- `tests/integration/test_admission_spawn_refusal.py::TestExternalAdmission::test_deny_blocks_real_spawn_with_or_without_native_policy`
- `tests/integration/test_admission_spawn_refusal.py::TestExternalAdmission::test_missing_engine_refuses_and_audits_unavailable_even_with_fail_open`

**Known limits**

- `PolicyHookRegistry(fail_open=True)` is a configured mode in which `first_decisive()` treats `HookVerdict.UNAVAILABLE` as having no opinion and falls through to `default_verdict` (`HookVerdict.ABSTAIN` by default): `tests/unit/test_external_policy_hook.py::TestPolicyHookRegistry::test_fail_open_on_error`.
- When no hooks are registered or all hooks abstain, `PolicyHookRegistry().first_decisive()` returns `HookVerdict.ABSTAIN` unless `default_verdict=HookVerdict.DENY` is configured: `tests/unit/test_external_policy_hook.py::TestPolicyHookRegistry::test_empty_registry_abstains`, `tests/unit/test_external_policy_hook.py::TestPolicyHookRegistry::test_custom_default_verdict`.
- Spawn-time enforcement runs only when a `PolicyHookRegistry` is supplied to `AgentSpawner`; when supplied, `AgentSpawner._enforce_external_admission()` requires an explicit `ALLOW` with no unsupported obligations and refuses `DENY`, `UNAVAILABLE`, or all-`ABSTAIN` responses regardless of `fail_open`: `tests/integration/test_admission_spawn_refusal.py::TestExternalAdmission::test_no_explicit_allow_refuses`, `tests/integration/test_admission_spawn_refusal.py::TestExternalAdmission::test_unsupported_allow_obligation_refuses`.
- `PolicyHookRegistry.first_decisive()` short-circuits in hook registration order: it returns `HookVerdict.DENY` on `UNAVAILABLE` only if no earlier registered hook returned `HookVerdict.ALLOW` (unlike `AgentSpawner._enforce_external_admission()`, which inspects all hook responses).
- `CedarHook` is a simplified matcher that extracts only `action == "<name>"` string literals. It ignores `principal` and `resource` head predicates (so matching `permit` and `forbid` rules apply globally to all principals and resources), a wildcard `forbid` without an `action == "<name>"` literal does not override a literal `permit`, and unsupported-clause detection checks only raw substrings (`when`, `unless`, `?principal`), so other Cedar constructs (`?resource`, `action in [...]`, entity literals) are not rejected while an action name containing one of those substrings raises `ValueError`.

## 5. Tamper evidence for persisted logs, seals, and lineage

**Invariant.** Post-write content alteration, interior record deletion, mid-line truncation, sealed-prefix or sealed-file-set tamper (including inserted or deleted `.jsonl` files), hash/HMAC linkage breakage, or head-anchored tail truncation causes verification to fail in the step journal, decision WAL, Merkle audit seal, lineage v2 parent/child store, or lineage run and fan-out chains; on tamper in lineage chains, `verify_lineage_chains()` increments `bernstein_lineage_tamper_total` (and emits `lineage_tamper_detected` and fires the alert webhook when `audit_log` and `sink` are passed), and `bernstein lineage verify` exits `2` on non-empty chain tamper.

**Enforced in**

- `src/bernstein/core/persistence/journal.py` (`Journal.open`, `JournalReader.verify`, `compute_step_hash`)
- `src/bernstein/core/persistence/wal.py` (`WALReader.verify_chain`)
- `src/bernstein/core/persistence/merkle.py` (`file_leaf_hash`, `compute_seal`, `verify_merkle`)
- `src/bernstein/core/lineage/v2_store.py` (`LineageV2Store.verify`)
- `src/bernstein/core/quality/janitor.py` (`verify_lineage_chains`)
- `src/bernstein/cli/commands/lineage_verify_cmd.py` (`lineage_verify_cmd`)

**Tests**

- `tests/unit/test_journal_chain.py::TestVerification::test_verify_detects_field_tamper`
- `tests/unit/test_journal_chain.py::TestVerification::test_verify_detects_head_mismatch`
- `tests/unit/test_journal_chain.py::TestRecoveryRevalidation::test_reopen_rejects_tampered_final_step_hash`
- `tests/unit/test_journal_chain.py::TestRecoveryRevalidation::test_reopen_surfaces_interior_break_not_deferred`
- `tests/unit/test_journal_chain.py::TestRecoveryRevalidation::test_reopen_rejects_seq_gap`
- `tests/unit/test_journal_chain.py::TestRecoveryRevalidation::test_fork_seed_rejects_corrupt_parent_journal`
- `tests/unit/test_wal.py::TestWALReader::test_verify_chain_detects_tampered_content`
- `tests/unit/test_wal.py::TestWALReader::test_verify_chain_detects_tampered_hash`
- `tests/unit/test_wal.py::TestWALReader::test_verify_chain_detects_broken_linkage`
- `tests/unit/test_wal.py::TestWALIntegration::test_corruption_in_middle`
- `tests/unit/test_wal.py::TestWALIntegration::test_deleted_entry_breaks_chain`
- `tests/unit/test_wal.py::TestWALIntegration::test_appended_forgery_detected`
- `tests/unit/test_merkle.py::TestFileLeafHash::test_v2_leaf_binds_whole_file_not_last_hmac`
- `tests/unit/test_merkle.py::TestVerifyMerkle::test_tampered_file_detected`
- `tests/unit/test_merkle.py::TestVerifyMerkle::test_deleted_file_detected`
- `tests/unit/test_merkle.py::TestVerifyMerkle::test_inserted_file_detected`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_parent_hmac_tampering`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_parent_prev_hmac_break`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_child_hmac_tampering`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_missing_child_file`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_orphan_child_file`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_content_address_mismatch`
- `tests/unit/test_lineage_v2_store.py::test_verify_wrong_key_fails_chain`
- `tests/unit/test_lineage_v2_store.py::test_verify_after_truncation`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_every_single_byte_flip_in_child_log`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_every_single_byte_flip_in_parent_log`
- `tests/unit/test_lineage_v2_store.py::test_verify_detects_renamed_payload_key`
- `tests/unit/test_lineage_v2_store.py::test_verify_rejects_unknown_key_appended_to_child_record`
- `tests/integration/test_lineage_tamper_detection.py::test_janitor_detects_tamper_and_emits_audit_event`
- `tests/integration/test_lineage_tamper_detection.py::test_janitor_calls_webhook_on_tamper`
- `tests/integration/test_lineage_tamper_detection.py::test_janitor_increments_prometheus_counter_on_tamper`
- `tests/integration/test_lineage_tamper_detection.py::test_lineage_verify_cli_exits_nonzero_on_tamper`
- `tests/integration/test_lineage_tamper_detection.py::test_janitor_detects_a_tampered_fan_out_branch`

**Known limits**

- Verification detects post-write modifications; it does not prevent filesystem writes or verify whether a record was accurate at write time.
- A torn (unparseable) trailing line in `Journal` or `WALWriter` is discarded during recovery so subsequent appends continue from the last valid entry: `tests/unit/test_journal_chain.py::TestRecoveryRevalidation::test_reopen_torn_final_line_recovers_last_good_entry`, `tests/unit/test_journal_chain.py::TestAtomicity::test_partial_line_is_not_observed_by_reader`, `tests/unit/test_wal.py::TestWALWriterEdgeCases::test_load_tail_corrupted_last_line`.
- `JournalReader.verify()` and `WALReader.verify_chain()` check unkeyed SHA-256 chains; rewriting an entire log from genesis with recomputed hashes passes chain verification unless checked against an external `expected_head` or a keyed or signed record.
- `LineageV2Store` is disabled by default (`is_v2_enabled` defaults to `False`), and verifying its HMAC chains requires the signing key: `tests/unit/test_lineage_v2_store.py::test_is_v2_enabled_default_false`, `tests/unit/test_lineage_v2_store.py::test_verify_wrong_key_fails_chain`.
- In `verify_lineage_chains()`, fan-out verification runs only when `run_graph` inputs are provided, and reaped worktrees or an incomplete `run_ids` map do not trigger tamper alerts: `tests/integration/test_lineage_tamper_detection.py::test_janitor_without_fan_out_inputs_sweeps_exactly_what_it_did_before`, `tests/integration/test_lineage_tamper_detection.py::test_janitor_does_not_call_a_reaped_fan_out_tampering`, `tests/integration/test_lineage_tamper_detection.py::test_janitor_does_not_call_a_missing_run_id_tampering`.
- Deleting whole trailing JSONL records passes verification when no external head anchor is checked: `JournalReader.verify(expected_head=None)` returns `ok=True` on any valid prefix (including a 0-line or missing file; tail truncation fails only when `expected_head` is supplied), `WALReader.verify_chain()` takes no `expected_head` parameter and returns `(True, [])` after trailing whole-line deletion or 0-byte truncation, and `LineageV2Store.verify()` returns `ok=True` if trailing complete `ChildBody` lines (`seq >= 1`) are removed while `seq=0` remains, or if trailing `ParentRef` lines are removed together with their unreferenced `children/<sha>.jsonl` files (whereas mid-line truncation fails as an undecodable record in `tests/unit/test_lineage_v2_store.py::test_verify_after_truncation`).
- Under the v2 Merkle seal scheme, each leaf pins the sealed file's `byte_len` prefix, so appending rows to an already-sealed `.jsonl` segment after sealing intentionally returns `valid=True` and reports those rows in `VerifyResult.post_seal_rows` (whereas inserting a new `.jsonl` file or modifying or truncating bytes within the pinned `byte_len` prefix fails).
- `verify_lineage_chains()` increments `bernstein_lineage_tamper_total` unconditionally on tamper, but appends `lineage_tamper_detected` and delivers the webhook alert only when `audit_log` and `sink` are passed (both default to `None`). In `bernstein lineage verify`, exit code `0` means intact chain, `2` means non-empty chain tamper (or an unresolved `--receipt-hash`), `1` means `NO ENTRIES` (including a file truncated to empty), `SEAL ONLY` (journal-head seal with no artifact provenance), or bad input, and `3` means verification could not run because the audit key is missing.

## 6. Historical identity validity checked at recorded decision time on replay

**Invariant.** `IdentitySpawnAnchor.reconstruct()` verifies the audit chain, frozen public key, and detached card signature, and evaluates the card validity window against the recorded `validated_at` timestamp rather than the live clock, so an anchored run reconstructs after later card expiry or key rotation while a recorded `validated_at` outside the validity window raises `IdentitySpawnAnchorError`.

**Enforced in**

- `src/bernstein/core/security/identity_spawn_anchor.py` (`IdentitySpawnAnchor.anchor`, `IdentitySpawnAnchor.reconstruct`, `_card_is_valid_at`)

**Tests**

- `tests/unit/security/test_identity_spawn_anchor.py::test_historical_anchor_survives_later_card_expiry`
- `tests/unit/security/test_identity_spawn_anchor.py::test_validation_timestamp_is_recorded_and_checked_historically`
- `tests/unit/security/test_identity_spawn_anchor.py::test_invalid_validity_window_is_rejected`
- `tests/unit/security/test_identity_spawn_anchor.py::test_live_key_rotation_does_not_orphan_historical_verification`
- `tests/unit/security/test_identity_spawn_anchor.py::test_physical_chain_tamper_blocks_reconstruction`
- `tests/unit/security/test_identity_spawn_anchor.py::test_signed_envelope_corruption_fails_digest_check`
- `tests/unit/security/test_identity_spawn_anchor.py::test_frozen_historical_public_key_tamper_fails_digest_check`

**Known limits**

- `validated_at` is taken from the caller-supplied `clock` at anchor time; `reconstruct()` does not verify `validated_at` against an external time-stamping authority.
- `_card_is_valid_at()` checks `created_at <= instant` and `expires_at > instant` when `expires_at` is non-zero (`expires_at == 0` is treated as non-expiring); it does not consult external revocation lists.
- `test_validation_timestamp_is_recorded_and_checked_historically` modifies `validated_at` past `expires_at` via a patched `chain.query` return value; physical edits to the audit log file fail earlier inside `chain.verify()` (`tests/unit/security/test_identity_spawn_anchor.py::test_physical_chain_tamper_blocks_reconstruction`).
