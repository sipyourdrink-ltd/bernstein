# Driving bernstein from inside an agent session

Operators increasingly drive tooling from inside their agent sessions
rather than a separate shell. Bernstein packages three surfaces for that:

1. A cross-vendor **agent skill** (`skills/bernstein-run/SKILL.md`, open
   `SKILL.md` format) that wraps the submit / status / verify loop.
2. A **plugin bundle** (repository root, manifest at
   `.plugin/plugin.json`) carrying the skill, slash commands, agent
   definitions, rules, and `.mcp.json` MCP-server registration.
3. **Registry listings**: the official MCP registry (`server.json`, PyPI
   package type) and a Docker MCP catalog entry
   (`packaging/docker-mcp/server.yaml`) backed by the signed GHCR image.

Installs are not fire-and-forget: every skill or plugin install can be
anchored as a content-addressed receipt in the lineage spine and HMAC
audit chain, so a run triggered from inside an agent session carries
provenance from install to artifact.

## The skill

`bernstein-run` teaches an agent session the three-step operator loop:

```bash
bernstein run -g "<goal>"          # submit
bernstein status                   # monitor
bernstein lineage verify <run_id>  # verify the run journal
bernstein audit verify             # verify the workspace audit chain
```

The skill body is host-neutral; per-host installation and worked
transcripts live in `skills/bernstein-run/references/worked-examples.md`.
Any agent CLI that executes shell commands can follow it against the same
bernstein install.

## Installing the skill into a host

```bash
# Copy the bundled skill into a host's default skills directory
bernstein skills package install --host claude --scope project
bernstein skills package install --host codex --scope user

# Host scans a different directory? Point at it directly
bernstein skills package install --dest .myhost/skills/bernstein-run
```

Supported `--host` values: `claude`, `codex`, `copilot`, `cursor`,
`gemini`. `--scope project` installs under the project root, `--scope
user` under the home directory. The defaults are conventions; `--dest`
always wins.

Every install writes an install receipt (`.sdd/skills/receipts/`) whose
canonical bytes are hashed into the `skills` lineage spine, and mirrors a
`plugin.install_receipt` event into the HMAC audit chain.

## Receipts for host-performed installs

When the host itself performs the install - a plugin checkout, a
marketplace install - anchor the resulting tree post hoc:

```bash
bernstein skills package install --dest <installed-dir> --record-only
```

## Verifying an install

```bash
bernstein skills package verify --host claude --scope project
bernstein skills package verify --dest <installed-dir>
```

`verify` re-hashes the installed tree; the recomputed content address
selects the receipt, and the install spine plus manifest hash are checked.
Because receipts are content-addressed, a tampered tree resolves to an
address with no receipt: tamper detection is structural, not a stored
comparison. Exit codes: `0` verified, `1` missing directory, `2`
attestation failure.

## Updating an install

Bumping an installed skill to newer content is not a plain overwrite. An
update binds the *prior* content address to the new one, so the
supersession is itself a chain-anchored fact:

```bash
bernstein skills package update --host claude --scope project
```

The update writes an update receipt (`.sdd/skills/updates/`) keyed by the
new content address, anchors it in the same `skills` lineage spine, and
mirrors a `plugin.update_receipt` event into the HMAC chain. A verifier
walks the update receipts newest to oldest and lands on the root
`plugin.install_receipt`, so the full supersession history of an installed
tree is reconstructable offline. An update onto a tree that was never
anchored is refused (run `install` first); an update whose source already
matches the installed tree is a no-op. Exit codes: `0` updated or already
current, `1` error.

`verify` (and `status`, below) transparently accept an updated tree: the
recomputed content address resolves to the update receipt, and the lineage
is required to chain back to a root install.

## Checking every install at once

`status` scans the default skill directory for each supported host and
scope, re-hashes any present tree, and proves it against its anchored
install or update receipt:

```bash
bernstein skills package status
bernstein skills package status --json
```

Exit codes: `0` every present install verifies (or none present), `2` at
least one present install failed verification. `--json` emits the
per-install verdicts for scripting.

## The plugin bundle

The repository root doubles as the plugin root:

- `.plugin/plugin.json` - manifest (version is stamped from
  `pyproject.toml` by `scripts/gen_distribution_manifests.py`).
- `commands/` - `/bernstein:run`, `/bernstein:status`, `/bernstein:stop`.
- `agents/orchestrator.md` - the orchestrator agent definition.
- `skills/` - the `bernstein-run` skill.
- `.mcp.json` - registers `bernstein mcp` as a stdio MCP server.

After a host installs the plugin, record its checkout with
`--record-only` (above) to get the same chain-verifiable receipt as a
CLI-performed install.

## Registry listings

- **Official MCP registry**: `server.json` lists the PyPI package and the
  GHCR image. It is regenerated from `pyproject.toml` and published by the
  `publish-mcp-registry` job in `.github/workflows/publish.yml`, so the
  listing version can never drift from the released package.
- **Docker MCP catalog**: `packaging/docker-mcp/server.yaml` is the
  submission payload; the image it references ships with BuildKit
  provenance, an SBOM, and a Sigstore build-provenance attestation. See
  `packaging/docker-mcp/README.md` for the maintainer flow.

## See also

- [MCP server](../mcp/server.md) - the tools the server exposes.
- [Plugin SDK](plugin-sdk.md) - extending bernstein itself.
- [Lineage](../lineage.md) - the spine that anchors install receipts.
