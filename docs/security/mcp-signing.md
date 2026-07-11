# MCP server signing and supply-chain scan

Third-party MCP servers are the next big supply-chain attack surface
for agentic apps. CVE-2025-6514 alone compromised ~437K developer
environments through a single unsigned `mcp-remote` package. Bernstein
treats every MCP server load as a potentially-hostile binary and runs
two gates before the manager spawns it.

| Gate | Source | Purpose |
|------|--------|---------|
| Ed25519 manifest signature | `mcp_verifier.py` | Prove the manifest was signed by a trusted publisher |
| Static supply-chain scan | `mcp_scanner.py` | Catch the four attack classes that drove the OpenClaw 433+ CVE corpus |
| Strict / warn-only enforcement | `mcp_signing_policy.py` | Decide what to do when one of the gates fails |

---

## Manifest signing (Ed25519 + JCS)

Every MCP server ships with an `mcp-server.yaml` (YAML or JSON) and
a detached `mcp-server.sig`. The manifest carries the publisher
fingerprint:

```yaml
name: example-mcp
version: 1.4.0
publisher:
  name: Example Org
  fingerprint: ed25519/abcd1234...
content_hash: sha256/<hex>
```

The signing path is:

1. The verifier parses the manifest and structurally validates it
   (name, version, publisher block, fingerprint shape, optional
   `content_hash` prefix). A malformed manifest fails closed with
   `BAD_MANIFEST` before any crypto runs.
2. `canonicalize_manifest()` emits RFC 8785 JCS bytes over the
   `{typ, name, version, publisher, content_hash}` body. The `typ`
   tag is bound to `mcp-server-manifest+ed25519` so a signature
   minted for a different JWS context cannot replay here.
3. The detached Ed25519 signature is verified against the publisher's
   PEM, resolved by fingerprint from the operator's `publisher_keys`
   map.
4. The publisher fingerprint must appear in the operator's
   `trusted_publishers` set; otherwise the verdict is
   `UNTRUSTED_PUBLISHER` even if the math is correct.
5. When the caller passes `bundle_bytes` and the manifest declares a
   `content_hash`, the bytes are hashed and compared. A mismatch is
   `CONTENT_HASH_MISMATCH`.

### Verdicts

| Verdict | Meaning |
|---------|---------|
| `ok` | Signed, signature valid, publisher trusted, hash matched (when present) |
| `unsigned` | Empty signature on a manifest the policy expected to be signed |
| `bad_signature` | Signature failed the Ed25519 verify call |
| `untrusted_publisher` | Math correct but the fingerprint is not in `trusted_publishers` |
| `bad_manifest` | Structural validation failed |
| `content_hash_mismatch` | Bundle bytes do not match `content_hash` |

The verdict is part of the structured result so log/UX surfaces and
audit records share one taxonomy.

### Sigstore deferred

The substrate in `core.security.sigstore_attestation` is
attestation-side only today. The `Sigstore` (Fulcio + Rekor)
verification path is deferred to a follow-up so the verifier review
surface stays tight. Ed25519 alone is a complete content-integrity
path; what defers is the second *who-published-this* signal.

---

## Static supply-chain scanner

`scan_mcp_bundle()` walks the source bundle and matches four attack
classes against regex/string patterns. Regex matching is deliberate:
an AST visitor would be more precise but only works against Python
sources, while MCP servers ship in Python, Node, Go, and Rust.

| Rule | Lineage |
|------|---------|
| Path traversal - `Path.resolve` missing in tool handlers | Anthropic Git MCP CVE-2025-68145 |
| Shell injection - `subprocess` with `shell=True` or unsanitised concatenation | OpenClaw CVE-2026-25253 |
| OAuth callback RCE - callback handlers without redirect-URI allowlist | mcp-remote CVE-2025-6514 |
| Scope escalation - token re-use with widened `scope=` | OpenClaw CVE-2026-32922 (CVSS 9.9) |

Each finding carries a `severity` (`info` / `low` / `medium` / `high`
/ `critical`), a `path:line` pointer, a CWE tag, and a remediation hint.

Plus a known-bad-package gate: package names on
`DEFAULT_KNOWN_BAD_PACKAGES` (currently the public CVE-tracked
`mcp-remote` and `openclaw-gateway-vulnerable` placeholders) are
flagged immediately. Operators extend the denylist via the
`known_bad_packages` argument so the in-tree feed can ride alongside
an external vulnerability source.

Lockfile-aware diffing is exposed at `scan_dependency_diff()` for the
manager to call when a lockfile is present; AST-level taint tracking
is deferred.

---

## Enforcement policy

`enforce_mcp_server_load()` is the single entry point the
`MCPManager` consults before spawning a third-party server. It applies
the operator's policy to the verification result and the scanner
findings.

```python
from bernstein.core.protocols.mcp.mcp_signing_policy import (
    MCPSigningPolicy, enforce_mcp_server_load,
)

policy = MCPSigningPolicy(
    strict=True,
    trusted_publishers=frozenset({"ed25519/abcd..."}),
    publisher_keys={"ed25519/abcd...": pem_bytes},
)

decision = enforce_mcp_server_load(
    server_name="example-mcp",
    manifest_yaml=manifest_text,
    signature_b64=sig_b64,
    bundle_files={"tools/exec.py": source_text},
    policy=policy,
    bundle_bytes=tarball_bytes,
)
```

### Strict vs warn-only

| Mode | Unsigned / bad sig / untrusted publisher | Critical scanner finding |
|------|------------------------------------------|--------------------------|
| `strict=True` | Refuse load (raises `MCPVerificationError`) | Refuse load |
| `strict=False` (warn-only) | Log warning, increment `mcp_unsigned_loaded_total`, allow load | Log warning, allow load |

The default for *new* environments is strict. The *first run* in an
existing environment defaults to warn-only so an in-place upgrade
does not break every running deployment until operators flip the
flag explicitly. Operators choose their on-ramp:

| Layer | Knob |
|-------|------|
| Per-process escape hatch | `BERNSTEIN_MCP_ALLOW_UNSIGNED=true` |
| Config file | `mcp.allow_unsigned: true` in `bernstein.yaml` |
| Code | `MCPSigningPolicy(strict=False)` |

The escape hatch logs loudly on each unsigned load and ticks the
`mcp_unsigned_loaded_total` counter, so misuse surfaces in audit even
when the load is permitted.

### Strict-mode refusal message

The remediation message names:

- the verdict in plain English (the same string the metric exporter
  records),
- the CLI verb the operator can run for full diagnostics
  (`bernstein mcp test <server-name>`),
- the override knobs (`mcp.allow_unsigned: true` and
  `BERNSTEIN_MCP_ALLOW_UNSIGNED=true`),
- up to three CRITICAL scanner findings with their CWE tags and
  source location.

The message stays log-friendly even when many findings fire - only
the first three show up in the head; the full list is on the
returned `MCPLoadDecision.scanner_findings`.

---

## Metrics

| Metric | Source |
|--------|--------|
| `mcp_unsigned_loaded_total` | Ticked on every unsigned load that the policy permits |

The counter is read via `unsigned_loaded_counter_value()` so the
metrics exporter and tests share one accessor.

---

## Related

- Source: `src/bernstein/core/protocols/mcp/mcp_signing_policy.py`,
  `mcp_verifier.py`, `mcp_scanner.py`
- [Capability matrix](capability-matrix.md) - the upstream gate that
  pins which MCP tool calls a role may dispatch
- [OWASP ASI04 - Agentic Supply Chain](owasp-asi.md) - the heuristic
  detector that delegates to this signature gate when callers
  populate `loaded_components` with `{name, signed}` entries
- [Lethal-trifecta security model](lethal-trifecta.md) - the structural
  exfiltration gate that runs alongside MCP signing
- RFC 8785 (JCS), RFC 8037 (Ed25519) - the canonicalisation and signing
  primitives the verifier reuses
