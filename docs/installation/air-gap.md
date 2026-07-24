# Air-gap installation

Bernstein is designed to run on systems that cannot reach the public
internet. The same wheel works in either mode - what changes is the
egress policy. This guide is for forward-deployed engineers (FDEs)
delivering Bernstein to sovereign customers, and for operators
maintaining an air-gap environment.

## When to reach for this

Use this path when:

- the host has no PyPI egress (regulated, sovereign, or simply offline)
- compliance asks for cryptographic provenance on every dependency
- adapters and MCP transports must fail closed on outbound packets
  unless an operator explicitly opens a destination
- the deployment must be auditable end-to-end without relying on a
  hosted control plane

The default Bernstein install on a developer laptop is fine without
any of this. The air-gap path adds three things on top of the same
binary: a wheelhouse, a signed manifest, and a runtime profile.

There are three pieces:

1. A **wheelhouse** - every wheel in Bernstein's pinned dependency
   closure plus the bernstein wheel itself, sitting in one directory
   ready for `pip install --no-index`.
2. A **signed manifest** - `MANIFEST.json` lists every wheel and its
   sha256, plus per-wheel `.sig` detached signatures the customer's
   compliance team verifies before install.
3. A **runtime profile** - `bernstein run --profile airgap` flips the
   default egress policy from "any" to "none". Network destinations
   that are explicitly approved are listed via `--allow-network`.

## On the build host (with internet)

You need `uv` and Python 3.12+ available. The build host is the only
machine that needs PyPI access.

```bash
# Build the wheelhouse (downloads every wheel in the closure + bernstein).
# Either the script directly...
python scripts/build_airgap_wheelhouse.py --version 1.10.0

# ...or the operator-friendly subcommand:
bernstein wheelhouse build --version 1.10.0

# Sign every wheel + the manifest with cosign
COSIGN_KEY=/secure/path/cosign.key \
  bash scripts/sign_airgap_wheelhouse.sh dist/airgap-wheelhouse/1.10.0
```

**cosign versions.** The signing script is tested against cosign
v2.4.x, v2.6.x, and v3.x, and adapts its flags to whichever is on
`PATH`. cosign v3 changed two `sign-blob` defaults
(`--use-signing-config` and `--new-bundle-format`) that are
incompatible with offline keyed signing; the script opts back out of
both when it detects a release that supports them. The detached
`<wheel>.sig` layout is identical across all three, so a bundle signed
under one version verifies under the others.

Result: `dist/airgap-wheelhouse/1.10.0/` containing

```
bernstein-1.10.0-py3-none-any.whl
bernstein-1.10.0-py3-none-any.whl.sig
fastapi-0.115.x-py3-none-any.whl
fastapi-0.115.x-py3-none-any.whl.sig
... (all transitive dependencies + their sigs) ...
MANIFEST.json
MANIFEST.sig
```

Copy this directory onto encrypted media. Bring the public key
(PEM) separately so the customer can verify in advance.

## On the customer site (no internet)

Mount the encrypted media. Verify before installing - never run
`pip install` against a wheelhouse you have not verified.

```bash
# 1. Confirm checksums against the manifest, signatures against the key.
#    Either form below works:
bernstein verify ./airgap-wheelhouse/1.10.0 \
  --ca-pubkey ./bernstein-release.pub \
  --require-signatures
#  -- or, equivalently:
bernstein wheelhouse verify ./airgap-wheelhouse/1.10.0 \
  --ca-pubkey ./bernstein-release.pub

# 2. Install with no PyPI access.
python -m venv .venv && source .venv/bin/activate
pip install --no-index --find-links ./airgap-wheelhouse/1.10.0 bernstein

# 3. Sanity check.
bernstein --version

# 4. Self-check the air-gap posture.
bernstein doctor airgap
```

The verify step is non-zero on any sha256 mismatch or signature
failure and names the offending wheel in the error message.

`bernstein doctor airgap` runs a battery of self-checks against the
current shell, exits 0 only when every row passes, and is the
intended pre-flight before any first run. The checks are:

| Check | What it asserts |
| --- | --- |
| `airgap profile active` | `BERNSTEIN_PROFILE_MODE=airgap` is set on this process |
| `network policy deny-all` | The active policy is `none` or an explicit allow-list, not `any` |
| `policy blocks declared endpoints` | Every adapter's `external_endpoints` is currently rejected |
| `MCP catalog all-off` | The user MCP config has no enabled bernstein-managed entries |
| `memo store on local disk` | No residual cache at `~/.cache/bernstein/`; memo pinned to `.sdd/runtime/memo/` |
| `audit chain HMAC valid` | Every entry under `.sdd/audit/` chains correctly |
| `no external hostnames in runtime` | No public LLM endpoint references in `.sdd/runtime/` |

```bash
bernstein doctor airgap          # human-readable
bernstein doctor airgap --json   # machine-readable for compliance evidence
```

`WARN` rows do not fail the run (e.g. legitimately operator-overridden
allow-list entries); only `FAIL` rows force the non-zero exit. The
JSON form is suitable as evidence in an air-gap pilot's evaluation
package.

### GPG verifier

Some sovereign customers prefer GPG over sigstore. The verifier is
pluggable - pick a backend at verify time:

```bash
bernstein wheelhouse verify ./airgap-wheelhouse/1.10.0 \
  --verifier gpg --keyring ./customer.gpg
```

The default backend is `auto`, which picks pure-Python crypto when
`--ca-pubkey` is supplied (Ed25519, ECDSA P-256, or RSA-PSS), falls
back to cosign with the same key, then to GPG when `--keyring` is.
Switch explicitly with `--verifier crypto|cosign|gpg` when both a
PEM key and a GPG keyring are on disk and you need to pin the path.

### Sigstore build-provenance verifier

Every released wheel + sdist published from `sipyourdrink-ltd/bernstein`
carries a Sigstore [build-provenance attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds)
generated by `actions/attest-build-provenance@v2`. The attestation
proves the artefact was built by the maintainer's GitHub Actions
identity (keyless OIDC via Fulcio), is signed by a short-lived
certificate chained to the Sigstore root, and is recorded in the
public Rekor transparency log - all without a pre-shared cosign key.

Consumers verify using either the official GitHub CLI directly:

```bash
# Per-file. Pulls the matching attestation from the GitHub
# attestations endpoint and re-validates the cert chain + Rekor
# inclusion proof.
gh attestation verify ./airgap-wheelhouse/1.10.0/bernstein-1.10.0-py3-none-any.whl \
  --owner sipyourdrink-ltd
```

…or via Bernstein's `--sigstore` flag, which loops through every wheel
in the bundle:

```bash
# Default: chains AFTER the existing cosign + GPG flow. Missing
# attestations -> "ADVISORY" (exit 0). Hard signature failures ->
# exit non-zero. The cosign / GPG verifiers run unchanged.
bernstein verify ./airgap-wheelhouse/1.10.0 --sigstore

# Strict: missing attestation -> hard failure. Use this in compliance
# CI / pre-deploy gates when EVERY release artefact must carry
# provenance and a missing attestation is itself a finding.
bernstein verify ./airgap-wheelhouse/1.10.0 --require-sigstore

# Pin to a specific repo (defence-in-depth against an attacker
# typo-squatting another repo under the same org).
bernstein verify ./airgap-wheelhouse/1.10.0 --sigstore \
  --sigstore-repo sipyourdrink-ltd/bernstein
```

Air-gap sites that cannot reach `api.github.com` for the attestation
endpoint pre-download the bundles on a connected build host and pass
`--sigstore-offline`:

```bash
# On the build host (with internet):
gh attestation download \
  ./airgap-wheelhouse/1.10.0/bernstein-1.10.0-py3-none-any.whl \
  --owner sipyourdrink-ltd

# Each wheel ends up with a sibling <wheel>.sigstore bundle.
# Copy the entire directory onto the encrypted media as before.

# On the customer site (no internet):
bernstein verify ./airgap-wheelhouse/1.10.0 \
  --sigstore --sigstore-offline
```

Failure modes the verifier handles honestly:

| Situation | `--sigstore` (default) | `--require-sigstore` (strict) |
| --- | --- | --- |
| `gh` CLI not on PATH | Skip with "install GitHub CLI to opt in" | Hard failure, exit non-zero |
| Network unreachable / timeout | Skip ("treating as advisory") | Hard failure |
| No attestation for this artefact | Skip ("no attestation found") | Hard failure |
| Attestation present but invalid | **Hard failure** (always - never skipped) | Hard failure |
| Attestation valid but wrong owner | **Hard failure** (always) | Hard failure |

The Sigstore check is purely additive: turning it off (the default)
preserves the historical cosign + GPG + checksum behaviour exactly,
so existing scripts continue to work unmodified.

### Why Sigstore plus cosign?

The two cover different threat models. Cosign signs the wheelhouse
*manifest* with a long-lived org key the operator pre-distributes;
Sigstore attests the *build provenance* with a short-lived
certificate tied to the GitHub Actions identity that produced the
artefact. A compromise of either chain leaves the other intact.
For full FINOS AIGF `CTRL-MODEL-SUPPLY-CHAIN` coverage we run both.

## Running with `--profile airgap`

The profile flips the defaults that matter for air-gap:

- `--allow-network none` (deny every outbound)
- MCP catalog entries are treated as opt-in only
- Memo store path is pinned to `.sdd/runtime/memo/` (no `~/.cache/`)

The profile does not change the bernstein binary. The same wheel
runs both modes.

```bash
# Pure local-only run against a local Ollama instance.
bernstein run --profile airgap --allow-network 127.0.0.1:11434 \
  --goal "Refactor my-detection-rule.yml so the selection clause is stricter"
```

If a plan tries to use an adapter whose endpoint is not on the
allow-list, Bernstein refuses to spawn that agent and exits non-zero
with the destination in the error:

```
NetworkPolicyDenied: network egress denied by policy: api.cloudflare.com:443 (from adapter:Cloudflare Agents)
```

## Allow-list syntax

Repeat `--allow-network` for each rule:

| Token | Meaning |
| --- | --- |
| `127.0.0.1` | Loopback only |
| `10.0.0.0/8` | A whole CIDR block (internal cluster) |
| `ollama.local:11434` | One specific host:port |
| `none` | Explicit deny-all (the `--profile airgap` default) |
| `any` | Opt out of the gate - back-compat default outside `--profile airgap` |

Default outside `--profile airgap` is `any`, so existing scripts
keep working unmodified.

## Adapter network endpoints

Each adapter that dials a SaaS endpoint declares it on the class as
`external_endpoints: tuple[tuple[str, int], ...]`. The base
`CLIAdapter.enforce_network_policy()` consults the active policy on
spawn and raises `NetworkPolicyDenied` before the child process
starts. That is how `--profile airgap` keeps Claude Code, Codex,
Gemini, Cloudflare Agents, Devin Terminal, AWS Q Developer, and the
other cloud-backed adapters from leaving the perimeter without an
explicit allow-list entry.

Audit which adapters dial out by grepping the source:

```bash
grep -nE "external_endpoints\s*=" src/bernstein/adapters/*.py
```

Representative entries today:

| Adapter | Source file | Declared endpoints |
| --- | --- | --- |
| Claude Code | `src/bernstein/adapters/claude.py` | `api.anthropic.com:443` |
| Codex CLI | `src/bernstein/adapters/codex.py` | `api.openai.com:443` |
| Gemini CLI | `src/bernstein/adapters/gemini.py` | `generativelanguage.googleapis.com:443` |
| Cloudflare Agents | `src/bernstein/adapters/cloudflare_agents.py` | `api.cloudflare.com:443` |
| Devin Terminal | `src/bernstein/adapters/devin_terminal.py` | `api.devin.ai:443`, `cli.devin.ai:443` |
| AWS Q Developer | `src/bernstein/adapters/q_dev.py` | `*.amazonaws.com:443`, `*.aws.dev:443` |
| Junie | `src/bernstein/adapters/junie.py` | depends on configured BYOK provider |

Adapters that do not declare `external_endpoints` (Aider against a
local Ollama, the IaC Terraform/Pulumi wrapper, the generic adapter,
etc.) are pure local subprocesses - the network gate is a no-op for
them. The OpenAI Agents SDK and any HTTP path inside the orchestrator
itself flow through `bernstein.core.security.network_policy` and
honour the same policy as the adapters.

To enable one adapter under `--profile airgap`, allow-list its
endpoint(s):

```bash
# Anthropic only - every other adapter still fails closed.
bernstein run --profile airgap \
  --allow-network api.anthropic.com:443 \
  --goal "Tighten the alert-rule selection clause"
```

`bernstein doctor airgap` includes a check (`policy blocks declared
endpoints`) that walks every adapter's declared destinations through
the active policy and reports anything that would be allowed today,
so the operator can confirm the perimeter before the first run.

## Re-signing on the customer side

A customer who does not trust the upstream signing key (or wants
to layer their own audit) re-signs the wheelhouse with their own key:

```bash
COSIGN_KEY=/secure/customer-key.key \
  bash scripts/sign_airgap_wheelhouse.sh ./airgap-wheelhouse/1.10.0

# Bernstein verify accepts an alternative public key:
bernstein verify ./airgap-wheelhouse/1.10.0 --ca-pubkey ./customer.pub
```

The detached signature scheme means we never bury keys inside the
wheel artefacts themselves.

## Adding customer-internal wheels to the bundle

Customer-built wheels (private packages, internal forks) drop into
the same directory and get signed alongside the upstream wheels.
Re-run the sign step after copying. Update `MANIFEST.json` by re-
running `python scripts/build_airgap_wheelhouse.py` with the same
`--output` path so the manifest picks up the additions.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `pip install` resolves to PyPI anyway | Forgot `--no-index` | Always pass `--no-index --find-links <dir>` |
| `bernstein verify` reports `missing signature` | The directory was copied without `.sig` files | Copy the entire wheelhouse, including signatures |
| `bernstein verify --sigstore` reports `Sigstore Verify: SKIPPED -- gh CLI not on PATH` | GitHub CLI missing on the customer host | Install [`gh`](https://cli.github.com), or omit `--sigstore` and rely on cosign / GPG only |
| `bernstein verify --sigstore` reports `no Sigstore attestation found` | Wheel was published before the attest workflow landed (pre-1.10.5) | Either upgrade to a release built by the post-1.10.5 pipeline, or omit `--sigstore` for that wheel |
| `bernstein verify --require-sigstore` fails on an air-gapped host | The verifier is trying to reach `api.github.com` | Add `--sigstore-offline` and ship the `.sigstore` bundles alongside the wheels (use `gh attestation download` on the build host) |
| `NetworkPolicyDenied: ...` at adapter spawn | Endpoint not on allow-list | Add `--allow-network <host>` or pick a local adapter |
| `bernstein run` exits with `--profile` not recognised | Older bernstein version | Upgrade to ≥ 1.10.0 |

## Limitations

- The shipped wheelhouse build covers Linux x86_64. Other platforms
  (macOS, Windows, arm64) require building their own wheelhouse
  against the same pin set.
- Native deps (cffi, lxml) are pinned to `manylinux_2_28_x86_64`. If
  the customer's distro doesn't have that manylinux variant, rebuild
  on a closer base image.
- The signing key shipped is the Bernstein release key. Customers who
  want their own bundle layer must re-sign as shown above.
- `bernstein doctor airgap` reports state, not policy - use it to
  confirm the run was clean, not as a runtime gate.

## Related

- Source: `scripts/build_airgap_wheelhouse.py`,
  `scripts/sign_airgap_wheelhouse.sh`,
  `src/bernstein/cli/commands/{verify_cmd,wheelhouse_cmd,doctor_airgap_cmd}.py`,
  `src/bernstein/core/security/network_policy.py`,
  `src/bernstein/core/distribution/{verifier,sigstore_attestation_verify,doctor_airgap}.py`,
  `.github/workflows/{publish,auto-release}.yml` (`actions/attest-build-provenance@v2` step)
- [Enterprise evaluation](../ENTERPRISE.md) - what to verify before a
  pilot, including the local-only / air-gapped path
- [Regulator-class lineage](../compliance/regulatory-lineage.md) -
  tamper-loud audit on the produced artefacts
- [Capability matrix](../security/capability-matrix.md) - how the
  network gate composes with the other security primitives
