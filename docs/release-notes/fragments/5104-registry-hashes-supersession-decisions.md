## Content hashes, supersession tombstones, and decision journaling in DuplicateGuard

- Extended `DuplicateGuard` (`bernstein.core.registry_guard`) to compute and
  store a deterministic `sha256:...` implementation content hash alongside the
  registering `module_path` via `RegistrationEntry` and
  `compute_implementation_hash()`.
- Added `SupersededRegistrationError(ImportError, ValueError)`,
  `DuplicateGuard.supersede()`, and `mark_superseded_module()` so retired ids
  and modules fail closed at import or registration time while naming the
  canonical replacement id.
- Recorded every registry mutation (`register`, `unregister`, `supersede`) as a
  `RegistryDecisionRecord` with optional append-only JSONL persistence
  (`journal_path` / `attach_journal()`, `load_decision_journal()`, and
  `reconstruct_registry_state()`).
- Migrated `core.trigger_sources.registry.Registry` and
  `core.protocols.schema_registry.SchemaRegistry` onto `DuplicateGuard` and
  forwarded `implementation=` in `AgentRegistry.register_definition()`.
  Refs #5104.
