# Response-style profiles

Response-style profiles give per-role control over worker output
verbosity. A reviewer role can run `terse` while an investigator role
runs `verbose`, without hand-editing role prompt templates. The style is
resolved deterministically at spawn time and rendered into the worker's
system prompt as an addendum.

Source:

- `src/bernstein/core/agents/response_style.py` - resolution + rendering
- `src/bernstein/core/agents/spawner_core.py` - spawn-time wiring
- `src/bernstein/core/config/config_schema.py` - `response_style` field

## The three styles

| Style | Maps to mode profile | Effect |
|-------|----------------------|--------|
| `terse` | `fast` | Compresses prose; shortest worker output. |
| `balanced` | `smart` | Neutral house style (the default). |
| `verbose` | `deep` | Fuller narration and reasoning in prose. |

`balanced` is the neutral default and renders an **empty** addendum, so a
spawn with no declared profile is byte-identical to a pre-feature spawn.
Only `terse` and `verbose` inject an addendum.

The addendum body is the mode-profile preamble from
`templates/mode_profiles/{fast,smart,deep}.yaml`. No new prompt dialect
is introduced; the vocabulary reuses the existing conciseness scale.

### Style carve-outs

The style applies to prose only. Every non-empty addendum carries a fixed
carve-out block instructing the worker never to compress, truncate, or
restyle code blocks, commit messages, error text, or completion-contract
JSON payloads. Those are always reproduced in full.

## Configuring it

Declare `response_style` per role under `role_model_policy` in your seed
config (`bernstein.yaml`). The special `default` entry sets the fallback
for roles that do not declare their own:

```yaml
role_model_policy:
  default:
    response_style: balanced   # seed-level fallback
  reviewer:
    cli: claude
    model: sonnet
    response_style: terse      # reviewers run terse
  investigator:
    cli: claude
    model: opus
    response_style: verbose    # investigators run verbose
```

The value is validated against the closed vocabulary
(`verbose` / `balanced` / `terse`) at parse time. During seed loading,
availability is checked for styles declared under `role_model_policy`
(including its `default` entry). A missing template for a declared
non-neutral style raises a typed `ResponseStyleTemplateError` during seed
validation, not at spawn time. Seed validation does not check a future
`Task.metadata['mode']`; that per-task mode is resolved when a worker starts,
and a missing template it selects is reported while rendering that worker's
addendum.

## Template lookup and neutral rendering

For each lookup, Bernstein first checks whether the project has a
`templates/mode_profiles/` directory. If it does, that directory takes
precedence over the bundled directory; if it does not, the bundled directory
is used. The directory is chosen before checking for the individual mapped
file, with no per-file fallback: when a project directory is selected, a
mapped file must be present there whenever a style needs it.

`balanced` is the exception: rendering it returns an empty addendum before
looking up or reading `smart.yaml`, so it needs no template file. For example,
if the project's selected directory contains only `fast.yaml` (the `terse`
template):

- `terse` loads `fast.yaml` and renders its addendum.
- `verbose` needs `deep.yaml` and fails with `ResponseStyleTemplateError`
  because that file is missing. If `verbose` is declared in the seed, this is
  caught during seed validation; if chosen by task metadata, it is caught
  when that worker's addendum is rendered.
- `balanced` renders an empty addendum and succeeds without reading or
  requiring `smart.yaml`.

## Resolution order

The style for a given spawn is resolved in this fixed order; the first
source that supplies a valid style wins:

1. `Task.metadata['mode']` - accepts a style name (`verbose` /
   `balanced` / `terse`) or the equivalent mode-profile name
   (`fast` / `smart` / `deep`).
2. The role's own `role_model_policy.<role>.response_style`.
3. The seed default `role_model_policy.default.response_style`.
4. The built-in default, `balanced`.

Unknown values fall through to the next source rather than raising, so a
stale `Task.metadata['mode']` value cannot block a spawn. Config-level
typos are rejected earlier by the seed parser.

The same inputs always resolve to the same style and the same rendered
addendum, so the resolution is snapshot-testable and reproducible across
machines.

## Identity, ledger, and audit

When a profile is **explicitly** declared (anything other than the
built-in default), the SHA-256 of the rendered addendum is:

- folded into the task identity used by the deterministic schedule
  projection, and
- stamped onto the task's cost-ledger entry (`response_profile` plus
  `profile_content_sha256`) and audit trail.

That stamping is what makes per-profile cost attribution possible - see
[Per-profile cost attribution](profile-cost-attribution.md). A spawn that
falls through to the built-in `balanced` default is not folded into
identity, keeping unset-profile runs byte-identical to before.

## Related

- [Per-profile cost attribution](profile-cost-attribution.md)
- [Profile A/B harness](profile-ab-harness.md)
- [Agent mode profiles](../concepts/agent-mode-profiles.md)
