# Agent catalogs

`catalogs:` in `bernstein.yaml` points the orchestrator at agent
definitions living outside the built-in role templates under
`templates/roles/`. Every entry contributes `CatalogAgent` records to a
`CatalogRegistry`; when a task's role matches a loaded agent, that agent's
system prompt replaces the built-in role template for that spawn (see
[role_model_policy](CONFIG.md#role_model_policy---per-role-agent-configuration)
for the adapter/model side of per-role configuration - catalogs control the
*prompt*, not the CLI/model).

## Catalog types

| `type` | Reads | Produces a matchable agent from |
|---|---|---|
| `agency` | Agency-format Markdown (`msitarzewski/agency-agents` layout: one file per agent, division subdirectories) | every parsed file |
| `generic` | A directory of YAML files (configurable `field_map`) and/or `SKILL.md` files | `SKILL.md` files only - plain YAML entries carry no system-prompt field, so they remain metadata-only (role listings, not spawnable prompts) |
| `plugin` | The Claude Code plugin/subagent layout | every parsed `.md` agent definition |

```yaml
catalogs:
  - name: local-specialists
    type: plugin
    path: ./agent-catalog
    priority: 60          # higher priority wins role conflicts; default 50
    enabled: true          # default true
```

Multiple entries are allowed; they are tried in descending `priority`
order. An entry with `enabled: false` is parsed but never loaded.

## The `plugin` catalog type

Reads three on-disk shapes under `path`, matching what Claude Code itself
reads from a project or an installed plugin:

- Standalone `.claude/agents/*.md` files.
- `plugins/<name>/agents/<agent>.md` files under each plugin directory.
- An optional `.claude-plugin/marketplace.json` index. When present and
  valid, it scopes discovery to the plugins it lists (`{"plugins": [{"name":
  "my-plugin"}]}`); when absent or malformed, discovery falls back to
  scanning every `plugins/*` directory, so one broken index cannot blind
  the loader to agents already on disk.

Each `.md` file is YAML frontmatter followed by a Markdown body used
verbatim as the system prompt:

```markdown
---
name: Security Reviewer
description: Audits pull requests for security vulnerabilities.
model: opus
tools: [ruff, mypy, semgrep]
---

You are the Security Reviewer agent. Audit every diff for injection,
auth bypass, and secret leakage before it merges.
```

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Display name; also the source of the catalog id (`plugin:<slug>`). |
| `description` | yes | Used for fuzzy/affine role matching against a task description. |
| `model` | no | Preserved on the loaded `CatalogAgent`, empty string when absent. |
| `tools` | no | List of strings, preserved verbatim and appended to the spawned prompt as a "Preferred tools" hint when the agent matches. |

There is no `role` field in this frontmatter (Claude Code's own subagent
format has no such concept): the Bernstein role is inferred from `name` +
`description` against the canonical role vocabulary (`backend`, `qa`,
`security`, and so on), falling back to `backend` when nothing matches.

Malformed input - an unterminated frontmatter fence, invalid YAML, a
missing `name`/`description`, a `tools` value that isn't a list of strings,
an empty body, a `marketplace.json` that isn't valid JSON - is never a
silent skip. Every failure is collected as a named error (file path +
reason) and logged as a warning; agents that parsed cleanly still load.

## Source resolution (`source_kind`)

By default a `generic`/`plugin` entry's `path` is read directly, exactly as
before this field existed. Setting `source_kind` routes resolution through
the same source-kind vocabulary the
[skill catalog](skills-catalog.md#catalog-sources) already uses:

```yaml
catalogs:
  - name: local-specialists
    type: plugin
    path: ./agent-catalog
    source_kind: directory   # or "file" for a single agent-definition file
```

`source_kind: file`/`directory` resolve immediately against local disk.
`github`/`git`/`npm` are valid configuration - the schema accepts them -
but resolution is not implemented yet: loading such an entry logs a
warning containing the reason (`catalog source kind 'github' is not
implemented yet ... remote agent-catalog sources are tracked in #3973`)
and that one entry contributes no agents; other entries are unaffected.
Remote resolution (digest pinning, a lockfile, signature verification -
the same provenance discipline the skill catalog already ships) is
tracked separately.

## How a match reaches a spawned prompt

`catalogs:` entries are parsed into a `CatalogRegistry` at seed-config load
time. The orchestrator then loads every enabled `generic`/`plugin` entry's
agents into the registry's matchable pool before handing the registry to
the spawner - the same registry a task's `role` is matched against on
every spawn. A match's `system_prompt` replaces the built-in role template
for that task, and a `tools` list (when present) is appended as a
"Preferred tools" hint.

A registry built from an empty (or absent) `catalogs:` config is
unaffected: no `generic`/`plugin` entries means nothing to load, and the
built-in role templates keep working exactly as before.
