# Skill catalog with signed manifest installs

The skill catalog promotes the same browse / list / search / install /
upgrade / info / status surface that `bernstein mcp catalog` already
ships - this time for skill packs. Catalog entries point at installable
sources (github, git, npm, file, directory) and carry a content digest
plus an optional Ed25519 signature so the operator (or an auditor) can
prove what bytes landed in `.bernstein/skills/`.

## Commands

```
bernstein skills catalog browse
bernstein skills catalog list
bernstein skills catalog list-installed
bernstein skills catalog search <query>
bernstein skills catalog info <id>
bernstein skills catalog install <id> [--allow-unverified] [--refresh] [--scope project|user]
bernstein skills catalog upgrade <id> [--all] [--allow-unverified]
bernstein skills catalog uninstall <id>
bernstein skills catalog sync
bernstein skills catalog status
```

All commands honour `--scope project` (writes into
`<cwd>/.bernstein/skills/`) or `--scope user`
(`~/.bernstein/skills/`). The default is `project`.

## Catalog sources

No catalog network request is made until a `bernstein skills catalog`
command runs. The built-in source is the operator-published catalog at
`https://bernstein.run/skills-catalog.json`, with a read-only mirror at
`https://raw.githubusercontent.com/chernistry/bernstein-skills-catalog/main/skills-catalog.json`
used as a fallback when the primary is unreachable.
The URL is validated as HTTPS before fetch, and fetched payloads must
match the signed catalog schema before they are cached or used.

## Manifest schema

A catalog entry is a strict JSON object. Unknown fields reject the
fetch, identical to the MCP catalog schema:

```json
{
  "id": "code-review",
  "name": "code-review",
  "version": "1.0.0",
  "description": "Review code diffs and surface risk hot-spots.",
  "source": {
    "kind": "github",
    "repo": "acme/code-review-skill",
    "tag": "v1.0.0"
  },
  "content_digest": "<64-char hex SHA-256>",
  "signature": "<base64url Ed25519>",
  "verified": true,
  "tags": ["review", "security"]
}
```

Supported `source.kind` values: `github`, `git`, `npm`, `file`,
`directory`. Each variant maps onto the existing
[`plugin_installer`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/plugins_core/plugin_installer.py)
implementation; the catalog does not introduce new download or extract
logic.

## Signature workflow

1. The publisher generates an Ed25519 keypair via
   `bernstein.core.skills.catalog.generate_signer_keypair()` (thin
   wrapper around the lineage layer's existing primitive).
2. The publisher signs each entry with `sign_entry(entry, private_pem)`
   and attaches the base64url-encoded signature on the `signature`
   field.
3. The catalog publishes the matching public key on the top-level
   `signer_pubkey` field.
4. The install path runs `verify_entry(entry, signer_pubkey)`. An entry
   without a signature, or with a signature that does not verify, is
   refused unless the operator passes `--allow-unverified`. An
   unverified install still proceeds but the audit event records
   `manifest_signer_pubkey=null`.

The signed payload is the canonical JSON of the entry, deliberately
excluding the `signature` and `verified` fields so the signature is
neither self-referential nor sensitive to operator-side flags.

## Audit chain integration

Every install / upgrade / uninstall appends an HMAC-chained event under
`.sdd/audit/`, reusing
[`bernstein.core.security.audit.AuditLog`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/security/audit.py):

| Event type                 | Payload fields                                                                                                |
|----------------------------|---------------------------------------------------------------------------------------------------------------|
| `skill.catalog.fetch`      | `source_url`, `from_cache`, `revalidated`                                                                     |
| `skill.catalog.install`    | `manifest_url`, `manifest_sha256`, `manifest_signer_pubkey`, `install_id`, `prev_chain_digest`                |
| `skill.catalog.upgrade`    | `from_version`, `to_version`, `manifest_url`, `manifest_sha256`, `install_id`, `prev_chain_digest`            |
| `skill.catalog.uninstall`  | (none)                                                                                                        |
| `skill.catalog.sync`       | `lockfile_digest`, `lineage_receipt`                                                                          |

Reverting and re-running the chain pulls the identical manifest sha; the
install refuses if the upstream sha drifted (a guard against silent
upstream rewrites).

## Lockfile and lineage receipts

The lifecycle's `skills.lock` is extended with two additional TOML
arrays:

```toml
[[catalog]]
id = "code-review"
name = "code-review"
version = "1.0.0"
manifest_url = "github://acme/code-review-skill@v1.0.0"
manifest_sha256 = "..."
content_digest = "..."
install_id = "..."
chain_head = "..."
installed_at = "2026-05-21T00:00:00+00:00"

[[lineage_receipt]]
worktree_id = "..."
action = "install"   # one of: install, adopt, pin
entry_id = "code-review"
from_chain_head = "0000..."
to_chain_head = "..."
manifest_sha256 = "..."
timestamp = "2026-05-21T00:00:00+00:00"
```

Writes are atomic (`Path.replace` on a sibling `.tmp` file) so a
concurrent reader either sees the old or the new state - never a partial
write. Two parallel worktrees launched from the same chain head observe
identical lockfile digests; an upgrade applied to one worktree produces
a `RECEIPT_ADOPT` receipt that the sibling can consult to decide
deterministically between `RECEIPT_ADOPT` (re-run the install) or
`RECEIPT_PIN` (stay on the prior chain head).

## CI lineage gate

`bernstein.core.lineage.gate.check_skill_lockfile` extends the existing
lineage-v1 gate (it does NOT add a new gate). The check passes when
every `[[catalog]]` row's `manifest_sha256` is present in the audit
chain's known-good set; a row whose sha is not anchored fails CI.

```python
from bernstein.core.lineage.gate import check_skill_lockfile

result = check_skill_lockfile(
    Path("skills.lock"),
    frozenset(auditor.known_good_manifest_shas()),
)
if not result.ok:
    raise SystemExit("\n".join(result.failures))
```

## Cache and TTL

The fetcher caches the upstream catalog under
`.sdd/skills_catalog/catalog.json` (project-local). The cache TTL
defaults to 6 hours; operators override it via
`BERNSTEIN_SKILLS_CATALOG_TTL` (seconds). The cache and the audit log
share a single source of truth: a stale fetch on a 5xx upstream serves
the last validated copy instead of failing.

## Drift detection

`bernstein skills catalog sync` recomputes the on-disk content digest
for every installed catalog skill and reports rows that do not match the
lockfile. Drift indicates either a manual edit under
`.bernstein/skills/<name>/` or an upstream rewrite; either is
operator-actionable, never silently re-installed.
