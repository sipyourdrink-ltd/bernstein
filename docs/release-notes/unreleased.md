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

- Validate attachment manifests and preview their derived tasks without writing to the task store.
  ([#6912](https://github.com/sipyourdrink-ltd/bernstein/pull/6912))
- Define an optional sandbox backend contract for verified reattachment and removal.
  ([#6911](https://github.com/sipyourdrink-ltd/bernstein/pull/6911))
- Include tenant-scoped coordinator readiness observations in dashboard task status.
  ([#7022](https://github.com/sipyourdrink-ltd/bernstein/pull/7022))
- Compare two stored governance audit reports offline with `govern audit --since` and `--against`.
  ([#7029](https://github.com/sipyourdrink-ltd/bernstein/pull/7029))
- Let MCP gateways explicitly opt in to recording capability observations.
  ([#7036](https://github.com/sipyourdrink-ltd/bernstein/pull/7036))
- Export ISO/IEC 42005 impact-assessment evidence with `bernstein audit export --standard iso-42005`.
  ([#7085](https://github.com/sipyourdrink-ltd/bernstein/pull/7085))
- Find the first changed or missing property in a pair of receipt render captures.
  ([#7101](https://github.com/sipyourdrink-ltd/bernstein/pull/7101))
- Aggregate supplied run-conduct projections within a specified time window.
  ([#7102](https://github.com/sipyourdrink-ltd/bernstein/pull/7102))
- Attach the Sigstore provenance bundle to GitHub Releases.
  ([#6968](https://github.com/sipyourdrink-ltd/bernstein/pull/6968))
- Provide an opt-in failure sentinel for testing audit detection, recording, and notification.
  ([#6916](https://github.com/sipyourdrink-ltd/bernstein/pull/6916))

## Changed

- Select fleet instances based on explicit worker capabilities rather than generic matches.
  ([#6910](https://github.com/sipyourdrink-ltd/bernstein/pull/6910))
- Update shipped Web UI dashboard assets with React Query 5.105.1.
  ([#6933](https://github.com/sipyourdrink-ltd/bernstein/pull/6933))
- Expose doctor installation checks as governance-audit report producers.
  ([#6918](https://github.com/sipyourdrink-ltd/bernstein/pull/6918))
- Migrate the quality-gate template to the standardized `GatePlugin` interface.
  ([#6964](https://github.com/sipyourdrink-ltd/bernstein/pull/6964))
- Quarantine skills that fail initialization with a journaled reason instead of failing silently or
  crashing. ([#6917](https://github.com/sipyourdrink-ltd/bernstein/pull/6917))
- Capture MCP server capability snapshots only after completing the upstream handshake.
  ([#7016](https://github.com/sipyourdrink-ltd/bernstein/pull/7016))
- Report the coordinator version separately from the bridge version in MCP status.
  ([#7023](https://github.com/sipyourdrink-ltd/bernstein/pull/7023))
- Make the declaration quarantine check callable through the audit check registry.
  ([#7033](https://github.com/sipyourdrink-ltd/bernstein/pull/7033))
- Classify and assign dispatch tiers to generated tasks based on runtime requirements.
  ([#7034](https://github.com/sipyourdrink-ltd/bernstein/pull/7034))
- Derive reconciliation intent keys and check whether a fresh observation already satisfies the intent.
  ([#7040](https://github.com/sipyourdrink-ltd/bernstein/pull/7040))
- Cache snapshot metadata directory resolution once per listing for improved performance.
  ([#7048](https://github.com/sipyourdrink-ltd/bernstein/pull/7048))
- Report both implementation modules when a duplicate audit check is registered.
  ([#7051](https://github.com/sipyourdrink-ltd/bernstein/pull/7051))
- Update `agents` dependency to `^0.33.0` in the Cloudflare MCP server template.
  ([#6950](https://github.com/sipyourdrink-ltd/bernstein/pull/6950))

## Fixed

- Show registered remote worker nodes and their claimed tasks in the `bernstein live` AGENTS panel.
  ([#6753](https://github.com/sipyourdrink-ltd/bernstein/pull/6753))
- Route `provider=openai` requests directly through the OpenAI API client.
  ([#6884](https://github.com/sipyourdrink-ltd/bernstein/pull/6884))
- Validate and forward reasoning effort parameters before spawning tasks in the Codex adapter.
  ([#6899](https://github.com/sipyourdrink-ltd/bernstein/pull/6899))
- Keep tasks from different dispatch tiers in separate execution batches.
  ([#6905](https://github.com/sipyourdrink-ltd/bernstein/pull/6905))
- Use the configured task server in role prompts, prompt partials, and the manager task-API skill.
  ([#6924](https://github.com/sipyourdrink-ltd/bernstein/pull/6924))
- Preserve task dispatch tiers across REST API requests and PostgreSQL persistence.
  ([#6908](https://github.com/sipyourdrink-ltd/bernstein/pull/6908))
- Make the completion-signal timeout configurable in place of the fixed 120-second limit.
  ([#6923](https://github.com/sipyourdrink-ltd/bernstein/pull/6923))
- Build a verified removal proposal for one leaked sandbox lifetime.
  ([#6909](https://github.com/sipyourdrink-ltd/bernstein/pull/6909))
- Record MCP tool-signing gate outcomes in the audit chain.
  ([#6913](https://github.com/sipyourdrink-ltd/bernstein/pull/6913))
- Include full injected tool definitions per server when calculating spawn prompt token budgets.
  ([#6907](https://github.com/sipyourdrink-ltd/bernstein/pull/6907))
- Fix datasource query receipt recording on Windows by sanitizing directory paths containing colons in
  agent IDs. ([#6928](https://github.com/sipyourdrink-ltd/bernstein/pull/6928))
- Omit unsupported sampling parameters when the OpenAI Agents adapter uses GPT-5 mini with the Responses
  API. ([#6975](https://github.com/sipyourdrink-ltd/bernstein/pull/6975))
- Complete clean orphaned tasks when their verification gates already pass on the base commit.
  ([#6900](https://github.com/sipyourdrink-ltd/bernstein/pull/6900))
- Ensure standard input streams are read reliably across platforms using Click `open_file` in telemetry
  commands. ([#6989](https://github.com/sipyourdrink-ltd/bernstein/pull/6989))
- Prevent releasing other active leases by ensuring sandbox holders release only their own assigned
  leases. ([#7001](https://github.com/sipyourdrink-ltd/bernstein/pull/7001))
- Let the pipeline enforce the `required` flag for failures reported by a quality-gate plugin.
  ([#7003](https://github.com/sipyourdrink-ltd/bernstein/pull/7003))
- Quarantine malformed routine trigger declarations to isolate configuration errors without halting
  execution. ([#7007](https://github.com/sipyourdrink-ltd/bernstein/pull/7007))
- Journal quarantine events for invalid issue trackers discovered during workspace scanning.
  ([#7008](https://github.com/sipyourdrink-ltd/bernstein/pull/7008))
- Automatically quarantine runtime plugins that encounter initialization failures.
  ([#7009](https://github.com/sipyourdrink-ltd/bernstein/pull/7009))
- Apply configured trigger rules when processing incoming GitLab webhook events mapped to tasks.
  ([#7010](https://github.com/sipyourdrink-ltd/bernstein/pull/7010))
- Preserve MCP capability snapshots when concurrent writers update the same store.
  ([#7026](https://github.com/sipyourdrink-ltd/bernstein/pull/7026))
- Preserve explicitly declared dispatch tiers defined in YAML plan steps.
  ([#7027](https://github.com/sipyourdrink-ltd/bernstein/pull/7027))
- Verify that the inventory anchor names the inventory recorded by a governance audit report.
  ([#7038](https://github.com/sipyourdrink-ltd/bernstein/pull/7038))
- Fix snapshot resolution and creation when operating inside linked Git worktrees.
  ([#7045](https://github.com/sipyourdrink-ltd/bernstein/pull/7045))
- Reject duplicate tunnel driver registrations to prevent driver conflicts.
  ([#7046](https://github.com/sipyourdrink-ltd/bernstein/pull/7046))
- Handle shallow registration call stacks gracefully in registry guards without raising inspection
  errors. ([#7047](https://github.com/sipyourdrink-ltd/bernstein/pull/7047))
- Recover the run outcome before recording the run-quiescence observation.
  ([#7049](https://github.com/sipyourdrink-ltd/bernstein/pull/7049))
- Preserve pytest error outcomes when parsing test evidence in flaky test detection.
  ([#7025](https://github.com/sipyourdrink-ltd/bernstein/pull/7025))
- Update vulnerable glob and minimatch dependencies and hash-pin the affected pip installs.
  ([#6965](https://github.com/sipyourdrink-ltd/bernstein/pull/6965))
- Correct documented CLI commands, flags, and the benchmark Lambda link.
  ([#7079](https://github.com/sipyourdrink-ltd/bernstein/pull/7079))
