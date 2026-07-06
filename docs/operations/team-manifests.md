# Named team manifests

A team manifest is a single named, content-hashed unit describing a
working team: the role list, a model policy per role, a response profile
per role, coordination settings, and pinned SHA-256 digests of the role
templates the team spawns from. Manifests are referenced from
`bernstein.yaml` so a whole team configuration - including which template
bytes it spawns against - is pinned by one hash and can be proven
identical across operators.

> This is distinct from the [team-hub convention](../concepts/team-hub.md),
> which is a shared directory tree of agents/skills/rules. A team manifest
> is a single pinned `.toml` unit resolved by name.

Source:

- `src/bernstein/core/teams/manifest.py` - manifest model + canonical form
- `src/bernstein/core/teams/drift.py` - drift classification
- `src/bernstein/core/teams/audit.py` - audit + lineage recording
- `src/bernstein/cli/commands/team_cmd.py` - the `team` command group

## Where manifests live

Manifests are `templates/teams/<name>.toml`, discovered project-local
first (your workdir's `templates/teams/`) then the bundled defaults.
Reference one from `bernstein.yaml`:

```yaml
team_manifest: backend-crew          # by name (resolves the visible manifest)
# or pin the exact content:
team_manifest: backend-crew@a1b2c3... # name@sha256
```

A manifest carries `name`, `version`, `[[roles]]` (each with an optional
`model_policy` and `response_profile`), a `[coordination]` table
(`parallelism`, `review_chain`), and `[role_template_digests]` pinning each
role's on-disk template directory.

### Canonical digest

The manifest digest is the SHA-256 of a canonical TOML serialization
(UTF-8, LF line endings, sorted keys, defaults resolved), defined so two
operators can prove their team configurations match byte-for-byte.
Serializing a loaded manifest and reloading it is a fixpoint. Optional
`signature` / `signer_pubkey` keys are excluded from the canonical form
(they attest the content and cannot be part of it); a signed third-party
manifest is verified at load time and refuses to load on mismatch.

## `bernstein team list`

Lists every manifest visible from the workdir, with name, version,
truncated digest, roles, and source path.

```bash
bernstein team list
bernstein team list --workdir /path/to/project
```

## `bernstein team show`

Prints one manifest in full: digest, source, coordination settings, each
role's model policy and response profile, and the pinned role template
digests. It also prints the exact `team_manifest: <name>@<digest>`
reference to pin it.

```bash
bernstein team show backend-crew
```

## `bernstein team drift`

Recomputes the on-disk role template digests and compares them to the
manifest's pinned `role_template_digests`. This is the lockfile-vs-disk
check for a team: it proves the templates the team will spawn against are
still the ones the manifest pinned.

```bash
bernstein team drift               # check every visible manifest
bernstein team drift backend-crew  # check one
```

- **Exit code.** Exits `1` when any pinned role template drifted for an
  unexplained reason, so CI can gate on it. Exits `0` when clean.
- **Intentional changes are not drift.** A divergence explained by a
  receipted [role-template compression](template-compression.md) (its
  `templates.lock` row) is reported as `receipted template compression
  (intentional, not drift)` and does not fail the check.
- **Missing templates** are reported explicitly (the on-disk digest shows
  as `<missing>`).
- **Audit + lineage.** When an audit chain exists under `.sdd/audit`, a
  drift finding is recorded on the HMAC-chained audit log; resolved
  manifest references are lineage-recorded on a best-effort basis.

| Argument / flag | Default | Meaning |
|-----------------|---------|---------|
| `NAME` | all visible | Restrict the check to one manifest. |
| `--workdir DIR` | `.` | Project root to resolve manifests and templates from. |

## Related

- [Role-template compression](template-compression.md)
- [Response-style profiles](response-style-profiles.md)
- [Team-hub conventions](../concepts/team-hub.md)
