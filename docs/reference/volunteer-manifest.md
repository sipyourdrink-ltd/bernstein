# Volunteer project manifest

A public project opts into receiving volunteer work by committing one file,
`.bernstein/volunteer.json`. The file is the project's declared policy: which
commands a submission must pass, which paths a patch may touch, which hosts the
sandbox may reach, and how long a task may run.

Nothing else is required. There is no account to create, no service to register
with, and no key to hand over. The file living in the project's own repository
is what proves control of the project.

## The manifest is content-addressed

The manifest has a canonical digest, and that digest is the value a submission's
receipt bundle carries as `manifest_sha256`.

This is what makes a receipt mean something. A volunteer who ran their own,
weaker gates can produce a bundle that verifies perfectly against their own key
and proves nothing about the project's actual acceptance bar. Binding the
receipt to the policy closes that gap: a maintainer recomputes the digest from
the manifest at the commit the submission names, and equal digests mean the
volunteer ran the policy this project declared. A mismatch is a refusal with no
judgement call in it.

Two properties follow, and both are load-bearing.

**Formatting is not policy.** The digest covers the normalised manifest, not the
file's bytes. Reindenting the JSON or reordering its keys does not invalidate
outstanding receipts, because neither changes what the project declared.

**Unknown fields are carried, never dropped.** A field a worker does not
recognise is preserved verbatim and still participates in the digest. Dropping
it would let a project add a policy-tightening field that older workers silently
ignore while still producing a matching digest — a downgrade with a
valid-looking receipt stapled to it. Older workers warn that they are running
under a policy they can only partly apply, and the digest still differs from the
manifest without the field.

## Example

```json
{
  "version": 1,
  "license": "Apache-2.0",
  "gates": [
    ["uv", "run", "pytest", "-q"],
    ["uv", "run", "ruff", "check", "."]
  ],
  "allowed_paths": ["src/**", "tests/**"],
  "egress_allowlist": [],
  "sandbox": "microvm",
  "max_wall_clock_minutes": 30,
  "task_label": "volunteer-ok",
  "local_ok": true
}
```

## Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `version` | integer | yes | Schema version. Only `1` is accepted today. |
| `license` | string | yes | OSI-approved SPDX identifier, case-sensitive. |
| `gates` | array of argv arrays | yes | Commands that must pass before a submission may be opened. At least one. |
| `allowed_paths` | array of strings | no (default `[]`) | Repository-relative globs a patch may touch. Empty means repo-wide. |
| `egress_allowlist` | array of strings | no (default `[]`) | Bare hostnames the sandbox may reach, on top of the package-registry set the sandbox profile defines. |
| `sandbox` | `"microvm"` or `"container"` | yes | Minimum isolation the project accepts. |
| `max_wall_clock_minutes` | integer 1–1440 | yes | Per-task wall-clock ceiling. A donor may set a tighter budget; no project may ask for more than a day of someone's machine. |
| `task_label` | string ≤ 50 chars | no (default `"volunteer-ok"`) | Issue label marking a task as open to volunteers. |
| `local_ok` | boolean | no (default `false`) | Whether tasks are generally solvable by local models. |

Any other field is preserved and binds to the digest, and the loader warns that
it does not enforce it.

## Gates are argv, never shell strings

A gate is an argument vector — `["uv", "run", "pytest", "-q"]`, not
`"uv run pytest -q"`. A bare string is refused with an error naming the argv to
use instead. Two reasons, in order of weight:

1. The command originates in a repository the donor does not control. Handing
   attacker-influenceable text to a shell is the exfiltration path this program
   exists to close, and no quoting discipline makes it safe in general.
2. The clean-room re-run has to execute the *same* command the original run did.
   A shell string is re-parsed by whatever shell each side happens to have, so
   "same string" does not imply "same execution" across two machines. An argv is
   the command.

A project whose gate is a pipeline puts the pipeline in a script and names the
script. The script is then part of the repository — reviewable, and hashed with
everything else.

## Validation rules

The loader refuses, naming the field at fault:

- a `license` outside the accepted OSI set, including a case variant like
  `apache-2.0` — SPDX identifiers are case-sensitive, and a near-miss must fail
  loudly rather than pass as the license it resembles;
- an empty `gates` list, a gate written as a string, an empty argv, or an
  argument containing shell metacharacters (`| & ; < > $ \` \\` or a newline);
- an `allowed_paths` entry that is absolute, names a drive, starts at a home
  directory, or walks out of the repository root;
- an `egress_allowlist` entry that is a URL, carries a path, contains a wildcard,
  or is not lowercase — a wildcard hands back the surface the deny-all default
  removes, and two spellings of one host would hash differently;
- a `max_wall_clock_minutes` outside 1–1440;
- a `sandbox` value that is not `microvm` or `container`;
- an unsupported `version`. Unlike an unknown field, an unknown version means the
  document's shape may have moved under fields the loader believes it
  understands.

Parsing is all-or-nothing. A manifest that fails validation produces an error,
never a partially-populated policy — a manifest with valid gates and no valid
sandbox would be more dangerous than no manifest at all.

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://bernstein.dev/schemas/volunteer-manifest-v1.json",
  "title": "Bernstein volunteer project manifest",
  "type": "object",
  "required": ["version", "license", "gates", "sandbox", "max_wall_clock_minutes"],
  "additionalProperties": true,
  "properties": {
    "version": {"type": "integer", "enum": [1]},
    "license": {"type": "string", "minLength": 1},
    "gates": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "minLength": 1}
      }
    },
    "allowed_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
    "egress_allowlist": {"type": "array", "items": {"type": "string", "minLength": 1}},
    "sandbox": {"type": "string", "enum": ["microvm", "container"]},
    "max_wall_clock_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
    "task_label": {"type": "string", "minLength": 1, "maxLength": 50},
    "local_ok": {"type": "boolean"}
  }
}
```

`additionalProperties` is `true` on purpose: that is the forward-compatibility
path, and unknown properties bind to the digest rather than being ignored.

The schema states shape. It does not state the OSI set, the path-escape rules,
or the host rules, because those are not expressible as a JSON Schema anyone
would want to read — the loader is the authority on them, and the list above is
the description of what it does.

## Reading it from Python

```python
from bernstein.core.volunteer import load_manifest_from_repo

manifest = load_manifest_from_repo(repo_root)
print(manifest.digest)          # the value a receipt carries as manifest_sha256
print([str(g) for g in manifest.gates])
```

## A worked manifest: this repository's own

Bernstein is opted into its own program. The live file is
[`.bernstein/volunteer.json`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/.bernstein/volunteer.json),
and reading it alongside the field table is more useful than reading either
alone, because two of its values are deliberately not the strictest available.

**`sandbox` is `container`, not `microvm`.** A floor nobody can stand on is not
a floor. Most donors have a container runtime and no microVM, and a manifest
demanding `microvm` refuses those donors outright rather than degrading. The
sandbox profile still prefers the strongest backend the donor actually has, so
declaring `container` buys the user-namespaced variant wherever it exists and
loses nothing on a host that could have run a microVM.

**`gates` omits mypy, which this project runs on every CI job.** It runs it
advisory — the step ends in `|| true` and cannot fail a build. Listing a command
the project does not itself enforce would reject a submission that main would
have accepted, for a reason no maintainer would defend at review time. The rule
is: declare the bar you enforce, not the bar you would like to have.

**`allowed_paths` stops at `src/`, `tests/` and `docs/`.** Not because the other
trees are unimportant but because they are outside what review reliably catches:
a patch touching `.github/workflows/` runs with the repository's secrets the
moment it merges, and one touching `pyproject.toml` or `uv.lock` installs a
dependency on every machine afterwards.

`tests/unit/volunteer/test_bernstein_own_manifest.py` holds all three: the lint
gates must match CI's own `run:` lines word for word, mypy must stay absent, and
the allowed-path roots are asserted as a set, so a future pattern rooted anywhere
else fails without anyone having to have anticipated it.

## Source

`src/bernstein/core/volunteer/manifest.py`.
