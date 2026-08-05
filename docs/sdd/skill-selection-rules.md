# Skill selection rules (issue #3383)

## Where the layer sits

Skill selection has three layers, applied in order inside
`bernstein.adapters.skills_injector.inject_skills`:

1. **Role binding** (`ROLE_SKILL_MAP`): deterministic baseline, keyed off the
   agent role. Corpus-immune, coarse.
2. **Selection rules** (`bernstein.core.skills.selection_rules`, this design):
   operator-authored `templates/skills/selection-rules.yaml` mapping
   `owned_files` globs — optionally narrowed by `task_type` — to skill
   templates. Resolution is a pure function of `(tasks, rules)`.
3. **TF-IDF auto-route** (opt-in, `BERNSTEIN_SKILLS_AUTO_ROUTE`): corpus-coupled
   scoring. Rule-selected templates are passed to it as exclusions so it can
   neither re-score nor duplicate them.

Role binding wins for a template selected by more than one layer; each
selection records its layer in the activation log (`trigger_source`:
`role-binding`, `rule`, `auto-route`).

The layer exists because the auto-route's IDF weights depend on corpus
document frequencies: adding one unrelated template to `templates/skills/`
can shift which skills a long-stable task receives. Rules give an operator a
corpus-immune, task-shaped binding whose output replays identically for
identical inputs.

## Why task types are matched by token, not by enum identity

The import-linter contract "Adapters must not import scheduler internals"
forbids `bernstein.adapters` from importing `bernstein.core.tasks` — directly
or transitively. The skill injector (adapters layer) imports this module, so
this module must not import `bernstein.core.tasks.models.TaskType`; the
injector's own local `Task` protocol exists for the same reason.

Task types are therefore matched by their lowercase value tokens
(`standard`, `upgrade_proposal`, `fix`, `research`): rules store the token,
and the resolver normalizes a task's `task_type` through
`getattr(value, "value", value)`. The restated token set cannot drift
silently — `test_known_task_type_tokens_track_the_scheduler_enum` pins it
against the real enum (tests import the enum freely; the contract binds
`src/` only).

Normalization fails closed: an *absent* `task_type` defaults to `standard`
(the injector's protocol does not carry the field), but a present-yet-
unrecognized value matches no `task_type`-scoped rule at all. Coercing an
unknown type to `standard` would inject operator-authored skills into tasks
that are explicitly not standard.

## Containment

A rule's template name is a bare file name resolved inside the skills source
directory, never a path: absolute entries, `..` traversal, and separator
components (posix or windows) are rejected at load. Without that, a rule
table could point the injector at any readable file on the host and have its
content injected as if it were a vetted template.

## No-table fast path

With no `selection-rules.yaml` present, behaviour is byte-identical to a
rule-less install: a single existence stat guards the layer and the loader
is never invoked. This is the merge-deciding property of the feature and is
pinned by `test_no_rule_table_is_byte_identical_noop`.

Operator-facing schema and examples: `docs/operations/skill-selection-rules.md`.
