# Seal anchors (`bernstein seal`)

`bernstein replay <run> --verify` proves a run's artifacts still hash to the
head the run sealed, to whoever holds the audit key. A seal anchor witnesses
that head outside this install, so a reviewer can also check *when* it
existed and whether a re-sealed rewrite would be visible.

| Command | What it does |
|---|---|
| `bernstein seal publish <run> --tsa-url <url>` | Ask an RFC 3161 TSA to timestamp the recomputed head; store `seal_anchor.json` beside the run journal |
| `bernstein seal publish <run> --token <file.tsr>` | Store a TSA reply obtained on another host (air-gapped operators) |
| `bernstein seal verify <run> [pins]` | Re-check the stored anchor offline; never opens a socket |

`publish` is the only command that can reach the network, and only when you
name `--tsa-url`. `verify` is always offline.

## Anchor kinds

`seal_anchor.json` carries an `anchor_kind`:

- `rfc3161` - a TSA token whose `messageImprint` covers the sealed head.
- `transparency-log` - an RFC 6962 inclusion proof: the leaf
  `SHA-256(0x00 || raw head bytes)`, `tree_size`, `audit_path`, the log's
  signed tree head, and a `log_public_key` hint.

## Verifying offline

`seal verify` recomputes the run's journal head, confirms the anchor
witnesses exactly that head, then dispatches on `anchor_kind`. Each kind
needs a trust root that you supply, never one read from the artifact:

| Anchor kind | Pin you pass | Without it |
|---|---|---|
| `rfc3161` | `--rfc3161-trusted-tsa-bundle <roots.pem>` | `unverifiable` |
| `transparency-log` | `--log-public-key <hex>` (repeatable) | `unverifiable` |

```bash
bernstein seal verify run-42 --log-public-key 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
```

### `--log-public-key`

- Lowercase or uppercase hex of the raw 32-byte Ed25519 key: exactly 64 hex
  characters. A malformed value is rejected as a usage error (exit 2) before
  any anchor is read.
- Repeat the flag to accept several logs or a key rotation.
- The `log_public_key` inside `seal_anchor.json` is only a hint for choosing
  among your pins. If it names a key you did not pin, the anchor is
  `invalid`, so a self-signed forged anchor never verifies. Several pins
  with no hint in the anchor are ambiguous and also report `invalid`.
- With no pin at all, a transparency-log anchor reports `unverifiable`,
  never a pass. An RFC 3161 trust bundle is not a fallback for a log anchor.

## Verdicts and exit codes

| Status | Meaning | Exit |
|---|---|---|
| `verified` | Anchor witnesses the current head under a pinned root | 0 |
| `mismatched` | The journal head moved since the anchor was issued | 1 |
| `invalid` | Token, inclusion proof, tree-head signature, or pin selection failed | 1 |
| `unverifiable` | No trust root pinned for this anchor kind | 1 |
| no anchor | The run was never anchored | 1 |

`--json` emits the verdict as JSON for scripts.

The standalone `bernstein-verify` wheel applies the same transparency-log
rules (operator-pinned key, embedded key as a hint) without importing
`bernstein`.

## Out of scope today

Publishing to a transparency log (`seal publish --log`), SCITT registration,
and dual anchors are tracked on #6208. `seal verify` checks a
`transparency-log` record that was produced elsewhere.
