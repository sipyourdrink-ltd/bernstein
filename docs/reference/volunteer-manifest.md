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
  "local_ok": true,
  "status": "active"
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
| `status` | `"active"` or `"paused"` | no (default `"active"`) | Whether the project is currently accepting volunteer work. A paused manifest still loads, validates, and digests, so older workers can keep producing receipts against the same policy digest; `browse` is the place a paused project drops out of the donor's view. |

Any other field is preserved and binds to the digest, and the loader warns that
it does not enforce it.

## What a glob in `allowed_paths` matches

Small on purpose, and the same language a patch is judged against wherever the
question comes up.

| | |
|---|---|
| `*` | any run of characters inside one path segment, including none |
| `?` | exactly one character inside one path segment |
| `**` | zero or more whole segments, and only as a complete segment |
| anything else | itself, including `[` and `]` |

Three consequences worth stating, because each is a place a scope can be wider
or narrower than its author meant.

**`*` stops at a separator.** `src/*` admits `src/a.py` and refuses
`src/deep/b.py`. The `fnmatch` in most standard libraries does not draw that
line, and a scope written expecting it to would admit the whole tree.

**A pattern is not a prefix.** `src` admits a file named `src` and nothing
under it; `src/**` is how a subtree is admitted. This is the rule people expect
to work the other way round.

**There are no character classes.** `[abc]` is three literal characters, so a
half-open bracket in a committed manifest cannot change what the rest of the
pattern means.

An empty `allowed_paths` admits everything — that is what a project which never
declared one carries, and it is the reason the list defaults to empty rather
than to a starter set someone would have to widen.

## A glob match is not the whole check

The matcher compares path *spellings*. That is exactly right for a matcher and
not enough on its own: `docs/../src/secrets.py` is a spelling `docs/**` matches,
and `src/plugins/x.py` matches `src/**` whether or not `src/plugins` is a
symlink pointing out of the checkout.

So a patch is judged in three steps, and the globs are the last one:

1. every path the patch names must be a usable repository-relative path — no
   `..` component, no absolute path, no drive letter, no NUL byte;
2. it must still resolve inside the worktree once symlinks are followed;
3. only then are the globs consulted.

A refusal says which step tripped and which paths tripped it, so "outside
`allowed_paths`" always means the globs, and never a traversal wearing their
clothes.

Three kinds of change print no hunk at all, and each is a way to touch a file
without editing it: a content-preserving rename or copy, a mode change
(`chmod +x`), and a binary file. All three are read out of the patch and
checked like any other path.

Matching is case-sensitive on every platform. On macOS `SRC/mod.py` and
`src/mod.py` open the same file; on Linux they are two files. A scope written
for one must not admit the other, so the case variant is refused — the answer
that is correct on both.

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

## What a gate run produces

The gates are re-run against the patch before anything is submitted, in the
order the manifest lists them, sharing one wall-clock budget rather than getting
one each — otherwise a manifest could multiply a donor's ceiling by declaring
more gates. The first failure stops the run: the gates after it cannot change
the outcome, and the machine is not ours.

Each gate's environment is built from the [sandbox
profile](volunteer-sandbox.md), never inherited from the donor's shell. A gate
is a command chosen by a repository the donor does not control.

The run then ends one of two ways, and there is no third:

- **a signed receipt bundle**, carrying this manifest's digest as
  `manifest_sha256`, the sandbox profile's digest, the patch, and every gate's
  command, exit code, and log;
- **a refusal record**, carrying a stable reason code and no bundle at all.

There is deliberately no bundle marked failed. A signed bundle is a claim that
the work is acceptable, and one that says otherwise in a boolean field is a
misreading away from being treated as a pass.

| Reason code | What happened |
|---|---|
| `patch_path_not_repo_relative` | A patch path is absolute, drive-qualified, or carries a `..` component |
| `patch_path_escapes_workspace` | A patch path resolves outside the worktree once symlinks are followed |
| `patch_outside_allowed_paths` | A patch path is well-formed and contained, and no glob admits it |
| `patch_names_no_path` | The patch is not empty but names no file this build can read |
| `profile_manifest_mismatch` | The sandbox profile was derived from a different manifest |
| `gate_failed` | A gate finished on time with a non-zero exit status |
| `gate_wall_clock_exceeded` | A gate was killed by the wall clock |
| `gate_budget_exhausted` | The budget ran out before a declared gate could start |
| `gate_not_executable` | A gate's program could not be run on this machine at all |

Codes are append-only. A donor fleet counts refusals by code, and renaming one
silently resets whatever was counting it.

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
    "local_ok": {"type": "boolean"},
    "status": {"type": "string", "enum": ["active", "paused"]}
  }
}
```

`additionalProperties` is `true` on purpose: that is the forward-compatibility
path, and unknown properties bind to the digest rather than being ignored.

The schema states shape. It does not state the OSI set, the path-escape rules,
or the host rules, because those are not expressible as a JSON Schema anyone
would want to read — the loader is the authority on them, and the list above is
the description of what it does.

## Checking it before you commit it

```bash
bernstein volunteer verify                 # this checkout
bernstein volunteer verify /path/to/repo   # somewhere else
bernstein volunteer verify --json          # for a CI step
```

The command loads the file through the same loader a donor's worker uses, so a
manifest that verifies here is one a worker will accept. On a rejection it names
the field and exits non-zero; the field name is the one in the table above, not a
line number, because the failure is a policy failure rather than a syntax one.

It prints the digest, which is the value a submission's receipt carries as
`manifest_sha256` — this is how a maintainer learns what their submissions will
be checked against, without recomputing it by hand.

It also prints **reachable hosts**, which is wider than the `egress_allowlist`
you wrote. An empty allowlist reads as "no network" and is not: the sandbox
profile adds the package registries, or the declared gates cannot install
anything. Seeing the real set is usually the moment a maintainer decides whether
to vendor dependencies.

## Reading it from Python

```python
from bernstein.core.volunteer import load_manifest_from_repo

manifest = load_manifest_from_repo(repo_root)
print(manifest.digest)  # the value a receipt carries as manifest_sha256
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
