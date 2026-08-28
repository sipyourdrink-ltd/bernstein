# Team-hub convention paths

A team hub is a directory tree that ships shared agents, skills,
and rules across multiple repositories without symlinks. The
convention pins one manifest filename (`team-hub.yaml`) and one
sub-directory (`team/`) so a hub can be detected by inspection.

## Why it exists

Multi-repo projects accumulate the same role templates, skill
packs, and prompt rules in three places at once. Symlinking one
canonical copy into every repo works on Linux but breaks on
Windows checkouts and on shared CI runners that copy worktrees
between machines. A convention-driven hub solves both: the
manifest enumerates exactly which paths the hub publishes, and
every consumer mirrors the directory layout instead of resolving
links at runtime.

## Hub layout

```text
<hub-root>/
    team-hub.yaml          # required manifest (strict-validated)
    team/
        agents/<name>/      # role / agent templates
        skills/<name>/      # skill packs (SKILL.md inside)
        rules/<name>.md     # plain-text rules consumed by the planner
```

The manifest enumerates which entries the hub publishes. Three
buckets are recognised: `agents`, `skills`, `rules`. Each entry
is a relative path inside the hub; validation rejects absolute
paths and any entry containing `..`, so a manifest cannot name a
path outside the hub root.

## What ships today

The manifest schema and its parser. `parse_team_hub_yaml` reads
`team-hub.yaml`, strict-validates it, and returns a frozen
`TeamHubManifest`:

```python
from pathlib import Path
from bernstein.core.plugins_core.team_hub_manifest import (
    TeamHubManifestError,
    parse_team_hub_yaml,
)

try:
    manifest = parse_team_hub_yaml(Path("/path/to/hub-repo/team-hub.yaml"))
except TeamHubManifestError as exc:
    # exc.path is the manifest, exc.detail says what is wrong with it
    raise

print(manifest.name, manifest.version)
print(manifest.ships.agents, manifest.ships.skills, manifest.ships.rules)
```

`validate_team_hub_dict` takes an already-parsed mapping and
applies the same schema, for callers that read the YAML
themselves.

Manifest example (`team-hub.yaml`):

```yaml
name: acme-platform-hub
version: "1"
compatibility:
  bernstein: ">=3.18"
ships:
  agents:
    - reviewer
    - release-manager
  skills:
    - ci-discipline
  rules:
    - prefer-typing.md
    - no-stale-todo.md
```

`name`, `version`, and `compatibility` are required. `name` is a
lowercase slug (`^[a-z][a-z0-9-]*$`). `compatibility.bernstein`
is a PEP-440 style specifier string; it is held to being a
non-empty string, and nothing enforces its semantics yet.

## Failure modes

`TeamHubManifestError` carries the manifest path and a detail
string. It is raised when the file does not exist, cannot be
read, exceeds the 64 KiB cap, is not valid YAML, is not a YAML
mapping, names a `ships` bucket outside `agents`/`skills`/`rules`,
or fails schema validation.

The size cap exists so a pathological YAML input cannot exhaust
the parser before validation runs.

## Limitations

- **Resolution against disk is not shipped.** The manifest is
  validated and its entries are checked for escape at validation
  time, but nothing yet walks `team/` to resolve those entries
  into concrete files, and no consumer reads a hub during a run.
  Path resolution, clone/pull, and resolution-path merging are
  later slices.
- Bucket vocabulary is fixed at `agents`, `skills`, `rules`.
  Custom buckets are a follow-up.

## Related

- Manifest schema: `src/bernstein/core/plugins_core/team_hub_manifest.py`
- [Skill packs](../architecture/skills.md)
