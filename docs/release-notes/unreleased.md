# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Added

- Memory compaction now anchors `TierResult` and compact trace steps with pre-compaction source content and referenced artifact hashes (#3696). `TierResult` carries `source_content_hash` (SHA-256 over exact pre-compaction UTF-8 bytes) and `referenced_content_hashes` (mapping of referenced artifact paths to content hashes captured at compaction time, recording `"absent"` for missing files), which propagate to the `compact` `TraceStep` in the trace store and are checked via `verify_compacted_step` to detect and report post-compaction artifact modifications, deletions, or creations. Docs: [`docs/architecture/memory_tiers.md`](../architecture/memory_tiers.md).
- The journal verifier (`verify_journal` / `verify_events`) is now covered by the mutation gate at 72.5% kill rate (#3654). Tests assert on each component of `JournalVerifyResult` — `chain_consistent`, `coverage`, `identity`, `count`, `divergent_index`, `head` — so that a change zeroing out the count or inverting a verdict would be caught. Every strict-mode validation guard is exercised with the row field deleted, replaced with the wrong type, or replaced with the empty string. The negative controls are discriminating: a corrupted journal returns the specific rejection (prev-hash break vs event-hash mismatch, chain divergence vs partial coverage), not a blanket "something is wrong", so a verifier that rejects everything would fail the tests. The survivors are in non-verification code paths: retention logic, helper functions, and error message formatting.
- Volunteer tasks enforce `allowed_paths`, re-run the project's gates, and assemble the signed receipt (#4033). `finish_volunteer_task` takes the patch a run produced plus the project's manifest and returns either a `SignedResultBundle` or a `VolunteerRefusal` — never a bundle marked failed, because a signed bundle is a claim the work is acceptable and one that says otherwise in a boolean field is a misreading away from being treated as a pass. Scope is enforced before any gate subprocess starts, and a glob match is not the whole check: the same filesystem containment barrier the rest of the project uses refuses a path that is not repository-relative (`docs/../src/x.py` is a spelling `docs/**` matches) and a path that resolves out of the worktree through a symlinked component, and only then are the project's globs consulted through the one matcher shared with credential scoping. Matching stays case-sensitive on every platform, so a scope written for `src/` cannot be satisfied by `SRC/` on a case-insensitive filesystem. Gates run as argv under one wall-clock budget shared across them rather than one budget each, stop at the first failure, and take an environment built from the sandbox profile rather than inherited from the donor's shell. A passing run's bundle carries the manifest's own digest as `manifest_sha256` and the profile's digest as its sandbox identifier, continuing the manifest → profile → receipt chain, and its worker identity is derived from the signing key so the bundle cannot name one worker while the signature is by another. Refusals carry stable, append-only reason codes. Part of #3869. Docs: [`docs/reference/volunteer-manifest.md`](../reference/volunteer-manifest.md).
- One answer to "which paths does this diff touch" (`bernstein.core.diff_paths`). The Tier-3 auto-heal cordon and volunteer scope enforcement both fail open on a path an extractor misses, so the extraction moved out of `autofix/tier3.py` into a shared module alongside `path_scope`, and gained the shapes it was missing: a content-preserving rename or copy, a mode change, and a binary file each touch a file while printing no hunk at all, and were previously invisible to the cordon. Quoted non-ASCII paths are decoded rather than handed to a matcher as escape sequences, and an ambiguous `diff --git` header contributes every candidate split — the extractor over-approximates on purpose, since a spurious path costs a readable refusal and a missing one costs an unchallenged write. `bernstein.core.autofix.tier3.extract_paths_from_unified_diff` still resolves for existing callers.
- Parking a task now writes the grant-bound agent checkpoint the resume path has been checking for (#4043). Nothing in the shipped code produced an `AgentCheckpoint`, so the authority check `bernstein task resume` runs — recompute the grant hash from the role and refuse when the permission set has moved — had no input on a live run and passed by having nothing to inspect. `park_task` now writes the checkpoint after the suspend receipt exists, keyed per task rather than per adapter so two tasks parked on one adapter no longer evict each other, and hashes the same permissions the resume re-derives so the two sides agree. The role is read where it is actually recorded, the task log, and the owning run comes from `$BERNSTEIN_RUN_ID`; `--role` and `--parent-run-id` pin either explicitly. When no role can be sourced the checkpoint is written with an empty grant hash rather than one over the unrestricted default permission set, which the resume would re-derive identically — a checkpoint that reads as bound and can never refuse is worse than one that admits it is not bound. Part of #3649. Docs: [`docs/operations/durable-suspend-resume.md`](../operations/durable-suspend-resume.md).
- The OpenCode adapter pins its own tool permissions and can re-enter a prior session (#3676). The contract row for `opencode` read `unsupported | unsupported` on resume and dangerous mode, and the adapter matched it: the spawn was a fixed `opencode run -m <model> --format json <prompt>` with no permission configuration at all, so a worker inherited whatever the operator's personal `opencode` config resolved to and two operators running the same plan got different agent behaviour. Both axes now describe flags the adapter passes. Every spawn carries an explicit `OPENCODE_PERMISSION` policy derived from the declared dangerous-mode strategy — escalated alongside `--auto` when it permits unattended action, tightened to a deny policy when it does not — applied after the environment allow-list so the pinned policy is what the worker sees rather than the host's. Neither policy resolves to `ask`, which would hang a headless run indefinitely against upstream `anomalyco/opencode#36762` and surface as a timeout rather than a blocked permission. `--continue` re-enters the prior session on a continuation retry, which the per-task worktree makes unambiguous, and the adapter opts into the continuation path so the warm retry the resume axis already derives is backed by something real. The event channel deliberately stays `text-signals`: the CLI does emit NDJSON under `--format json`, but nothing consumes it yet, and consuming it is the remaining half of #3676.
- MCP server advertises repository URL, package version, and build provenance in its capability card and server info, sourced at runtime from packaging metadata rather than static literals (#3646).
- Russian README (`README.ru.md`), under the same drift gate as the other six translations. The language-links line now carries `Русский` in every README including the English source, so the switcher is reachable from whichever page a reader landed on rather than only from the English one. `bernstein readme-l10n verify` covers the new page like the rest: every section is bound to a hash of the English section it mirrors and the command blocks are compared byte for byte against the source, so a Russian page that falls behind an English edit fails the build naming the stale section instead of drifting quietly. The locale and its owner are registered in `[tool.bernstein.readme-l10n]`.
- A resume that clears its authority check now records that it did, as a `task.grant_continuation` journal row (#3835). The row binds the checkpoint's hash, its grant hash, and both chain heads — the suspend row's and the resume row's — so a verifier holding only a copy of the journal can chain the two halves of a parked run together instead of inferring the join from timestamps or reading `.sdd/runtime/`. `build_continuation_entry` had been present with no caller since it landed; this is what calls it. A park that wrote no grant, and a task old enough to carry no checkpoint, both produce no row, and the verifier reads that absence as a new run rather than as an attested continuation — silence never counts as evidence. The resume receipt's `journal_index` keeps naming the resume row now that a second row lands between the append and the receipt. Part of #3649; #3834 and #3836 remain.
- Export SOC 2 evidence pack to configured storage sinks via `export_soc2_evidence_pack` under canonical keys (#4148).
- Dashboard colour tokens record measured contrast ratios and are gated against WCAG AA thresholds dynamically from the stylesheet (#3589). Contrast ratios for all body text, metadata, semantic states, and pill variants are catalogued in [`docs/design/web-ui-inventory.md`](../design/web-ui-inventory.md) and enforced by `tests/unit/test_webui_contrast.py`.
- PostgresTaskStore and BaseTaskStore declare parameterized limit and offset bounds on list_tasks (#4157). PostgresTaskStore.list_tasks mirrors TaskStore.list_tasks by accepting optional limit and offset integer bounds and parameterizing them into the SQL query (LIMIT $n OFFSET $n) to prevent unbounded row fetching across database-backed deployments.
## Security

- The agent task-scope gate no longer depends on how FastAPI happens to store
  included routers (#4023). Both matcher sets are compiled from the live
  route table, and from FastAPI 0.137 `include_router` keeps a wrapper object
  in that table instead of copying the sub-router's routes into it — the walk
  saw objects with no `path`, enumerated nothing, and every matcher came back
  empty. The gate then failed in both directions at once: per-task routes
  registered through a sub-router (`/approvals/{task_id}/approve`, the review
  board's per-task decision route) lost their scope check entirely, while
  `/tasks/batch` and the other collection routes lost their exemption and were
  gated as though `batch` were a task id. The walk now lives in
  `core/routes/route_table.py`, descends through those wrappers, re-applies
  each mount prefix, and recognises a wrapper by shape rather than by
  importing a private class, so a future rename degrades to the older
  behaviour instead of raising on import. `tests/unit/test_route_table.py`
  asserts the real app's matcher sets are non-empty, so a framework change
  cannot empty them again while the rest of the suite still passes. The
  `fastapi<0.137` ceiling that #3979 added as a stopgap is removed.

- Issue text from a project a donor does not control is normalised before it
  can become an agent prompt (#4031). `sanitize_issue_text` in
  `core/volunteer/issue_sanitize.py` returns the title and body as one
  delimited block, closing the three channels through which text a reviewer
  never saw could reach the model. HTML comments are stripped in both
  spellings: closed `<!-- ... -->` across any number of lines, and an
  unterminated `<!--`, which opens a CommonMark HTML block whose end condition
  is never met, so the rendered page hides everything after it while the API's
  raw body carries all of it. Invisible characters are dropped explicitly
  rather than left to normalisation — NFKC removes none of the 170 `Cf` format
  characters, so U+200B, U+FEFF and U+202E RIGHT-TO-LEFT OVERRIDE all survive
  it, and a word a reviewer read as one word would otherwise reach the model as
  two. `Cc`, `Cf` and `Cs` characters are removed with newline and tab
  excepted, `\r\n` and a lone `\r` fold to `\n` first so that dropping a
  carriage return cannot glue two lines into one word, and NFKC then runs last,
  which leaves the block itself NFKC-normalised for anything downstream that
  hashes it. The block's fence is derived from the digest of its own content
  and re-derived until it does not occur there: deterministic, so the same
  title and body produce the same bytes in every process and replay stays
  byte-identical, and unforgeable, so a body containing the fence verbatim
  cannot close it early. `normalize_untrusted_text` exposes the same transform
  without the prompt fence. Nothing here asserts model behaviour, and the
  module imports `hashlib`, `re` and `unicodedata` and nothing else, pinned by
  an exact allowlist so it cannot quietly grow a route to a shell, the
  environment, or the network.

- The last hand-rolled filename join in `core/orchestration/worker.py` now
  goes through the shared containment helper (#4106). `_avoid_shim_line_overflow`
  derived its stdin-overflow prompt file as `prompt_dir / f"{session}.stdin-overflow"`
  with no check at all — the only site left in the file where a value reaching
  the function could name a file outside the intended directory. It is now
  `contained_path(prompt_dir, f"{session}.stdin-overflow", label="session id")`,
  the same helper #3802's sweep has been centralising everywhere else (most
  recently in #4095). This is defence in depth rather than a live hole: `session`
  has already passed `_SESSION_ID_RE` in `main()` before the function is ever
  reached, and the allowlists agree on characters, so nothing arriving through
  the CLI is newly refused. Two behaviours are pinned by tests: a session value
  the CLI would have rejected now raises `PathContainmentError` instead of
  naming a file, and an over-long session that passes the regex but exceeds the
  255-byte component cap now fails as `PathTooLongError` at the check instead
  of reaching `open()` as `OSError(ENAMETOOLONG)` (the same capacity-vs-
  containment distinction #4095 recorded for the pid-file sites).

- Health check task count is scoped to the caller's tenant (#4156).
- Stall escalation produces a degraded terminal receipt on a missing or empty
  event journal instead of raising (#3737). The kill already happened in that
  case; refusing to build the receipt left nothing in the chain to tell
  "terminated with a recorded cause" from "never concluded". The receipt now
  carries `journal_state` (`missing` / `empty`) in place of the reconstructed
  window, signs and anchors on the escalation spine like any other terminal
  receipt, and the `escalation.receipt` chain mirror records the same field, so
  the degradation is visible from the chain alone. `EscalationError` still
  raises for genuinely malformed input — a non-positive window, or a `fork_step`
  no snapshot event pins. The recorded absence stays falsifiable: verifying a
  receipt that claims `missing` against a run whose journal does hold entries
  fails and names the contradiction, rather than letting `journal_state` become
  a standing bypass of the window reconstruction every other receipt is held
  to. `bernstein escalation verify` reports these as `OK (degraded)` instead of
  claiming a window reconstructed from a journal that was never there.
- A review correction can now be filed as a convention receipt instead of free
  text (#3750). `file_review_correction()` in `core/knowledge/conventions.py`
  routes the correction through the existing `file_lesson()` store and binds
  `{rule_text_hash, subject_path, subject_symbol, base_commit_sha,
  assertion_ref, filing_finding_id, decided_by}` into one Ed25519-signed record
  anchored in the HMAC audit chain. Filing the same correction three times
  leaves one receipt at `version: 3`. A rule whose `subject_symbol` no longer
  resolves at HEAD (checked by AST for Python sources) moves to `expired` and
  appends the chain entry saying so, rather than quietly ceasing to apply — an
  expired rule and a rule that was never filed have to be tellable apart. A
  correction that contradicts one already in force is rejected at file time
  naming both receipt ids; two rules that merely touch the same file are not a
  contradiction and both file. `bernstein verify --memory-audit` covers the
  receipts: it runs the audit chain's own verifier before checking anchors, so
  a rewritten rule with a hand-appended log line behind it fails rather than
  passing a presence check. The rule set in force carries a `ruleset_hash` over
  what each rule demands — never over receipt ids or filing timestamps — so two
  installs with the same rules agree on the digest. Filing is still an explicit
  call: no orchestrator path invokes it yet, which
  [`docs/concepts/lesson-persistence.md`](../concepts/lesson-persistence.md)
  records as the remaining gap.
- Workspace merge order calculation is scoped to the caller's tenant (#4155).
