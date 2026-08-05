# Skill selection rules

A declarative, operator-authored rule layer that selects extra skill
templates for a spawn based on task facts. It sits between the two
selection mechanisms that already exist in the injector
(`src/bernstein/adapters/skills_injector.py`):

- **Role binding** (`ROLE_SKILL_MAP`): deterministic role -> skill
  mapping, always on.
- **TF-IDF auto-route** (opt-in via `BERNSTEIN_SKILLS_AUTO_ROUTE=1`):
  scores every template in the corpus against the task text. Useful,
  but *corpus-coupled*: adding an unrelated template to
  `templates/skills/` shifts document frequencies and therefore scores.

Selection rules are corpus-immune. Resolution is a pure function of
`(tasks, rules)` - no corpus statistics, no environment reads, no
ordering sensitivity - so the same tasks and the same rule table always
select the same templates, regardless of what else lives in the skills
directory. Implemented in
`src/bernstein/core/skills/selection_rules.py`.

## File location

The rule table is a YAML file named `selection-rules.yaml` living in the
skills source directory, i.e. sibling of the skill templates it names:

```
templates/
  roles/
  skills/
    bernstein-completion-protocol.md
    pytest-helper.md
    selection-rules.yaml      <- the rule table
```

Presence is checked with a single cheap stat at injection time. When the
file is absent, the loader is never invoked and injection behaves
exactly as if the rule layer did not exist.

## Schema

```yaml
rules:
  - owned_files: "src/api/*.py"     # REQUIRED: one glob or a list of globs
    task_type: fix                   # optional: standard | upgrade_proposal | fix | research
    skills:                          # skill template names (".md" suffix optional)
      - api-conventions
```

Axes:

- `owned_files` (required): fnmatch-style glob(s) matched against each
  task's `owned_files` entries.
- `task_type` (optional): one of the `TaskType` values (`standard`,
  `upgrade_proposal`, `fix`, `research`), case-insensitive. When both
  axes are present, **both must match on the same task**.

There is deliberately **no `role` axis** - role -> skill binding is
owned by `ROLE_SKILL_MAP`, and the schema rejects a `role` key at load
so the two layers cannot drift into conflict.

Multi-task semantics are a union: if any task assigned to the spawn
matches a rule, the rule's templates are selected. Hits are
deduplicated and ordered by rule position, then template name within a
rule.

## Fail-loud validation

The loader (`load_selection_rules`) validates the table at load and
raises `SelectionRuleError` naming the offending rule and the problem
for:

- invalid YAML or a top level that is not a `rules:` mapping;
- unknown keys on a rule (including the rejected `role` axis);
- a non-string / non-list `owned_files` value, or empty globs;
- an unknown `task_type` value;
- a rule naming a skill template that does not exist as
  `<skills_source_dir>/<name>.md`.

An empty file, `rules:`, or `rules: []` is an empty-but-valid table:
no rules, no error.

## Precedence

When the same template is selected by more than one layer:

```
role-binding  >  rule  >  auto-route
```

- A template selected by both role binding and a rule is injected once
  and keeps the `role-binding` trigger.
- Rule-selected templates are passed to the TF-IDF auto-route as
  excluded, so the auto-route neither re-scores nor duplicates them.

## Activation log

Rule-selected skills flow through the existing activation log
(`.sdd/skills/activations.jsonl`) with `trigger_source` set to `rule`:

```json
{"skill":"pytest-helper","role":"backend","task_id":"T-42","trigger_source":"rule","...":"..."}
```

alongside the existing `role-binding` and `auto-route` trigger sources.
