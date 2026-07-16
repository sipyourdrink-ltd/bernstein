# EU-residency customer setup (DeepSeek V4 + self-hosted Ollama / vLLM)

This guide walks a sovereign-customer compliance team through deploying
Bernstein under the **EU-residency profile**: DeepSeek V4 running on
RFC-1918 / loopback infrastructure inside the customer's perimeter,
with Bernstein's residency guard enforcing that no token leaves the
boundary.

## Audience

- Compliance officer signing off on Article-12 evidence.
- Platform engineer who'll deploy Ollama / vLLM and point Bernstein
  at it.
- Internal audit pulling proof that the inference call never crossed
  the EU boundary.

## What the residency guard actually enforces

Bernstein's [`OllamaAdapter`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/ollama.py)
calls `_is_self_hosted_endpoint(base_url)` on every `spawn()`. The
guard fires when **either** of the following is true:

1. The requested model is in `_EU_RESIDENCY_MODELS`
   (currently `deepseek-v4-flash`, `deepseek-v4-pro`).
2. The adapter was constructed with `eu_residency=True`.

When the guard fires, Bernstein refuses to spawn against any URL
whose host is not on the self-hosted allowlist:

| Allowed host shape           | Example                                |
|------------------------------|----------------------------------------|
| Loopback IPs                 | `127.0.0.1`, `::1`, `0.0.0.0`          |
| Loopback hostname            | `localhost`                            |
| RFC-1918 private ranges      | `10.x.x.x`, `192.168.x.x`, `172.16-31` |
| `*.internal` / `*.local`     | `vllm.internal:8000`                   |
| `*.svc` / `*.cluster.local`  | `ollama.svc.cluster.local:11434`       |

Any other host (public IP, hosted-API hostname, unrecognised FQDN) is
rejected with a structured `RESIDENCY_VIOLATION` error that names
both the offending endpoint and the model that triggered the guard.

## Deployment recipe

### 1. Stand up an Ollama / vLLM endpoint inside the perimeter

Two supported shapes, pick the one that matches your hardware budget:

#### a) Single-GPU Ollama for `deepseek-v4-flash`

`deepseek-v4-flash` is a 284B / 13B-active MoE model that fits on a
single H100 / A100. Pull and serve via Ollama:

```bash
# On a host inside the EU perimeter (RFC-1918 IP):
ollama pull deepseek-v4-flash
ollama serve  # listens on 0.0.0.0:11434 by default
```

Bind Ollama to a private interface only:

```bash
OLLAMA_HOST=10.0.0.5:11434 ollama serve
```

#### b) vLLM tensor-parallel for `deepseek-v4-pro`

`deepseek-v4-pro` is 1.6T / 49B-active and does NOT fit on a single
GPU. Deploy via vLLM tensor-parallel:

```bash
# On a multi-GPU host inside the EU perimeter:
python -m vllm.entrypoints.openai.api_server \
    --model deepseek-ai/deepseek-v4-pro \
    --tensor-parallel-size 8 \
    --host 10.0.0.5 \
    --port 8000
```

Both Ollama and vLLM expose the OpenAI-compatible `/v1/chat/completions`
endpoint that aider/litellm route through.

### 2. Generate the EU-residency profile

Drop a `eu_residency_profile.yaml` next to `bernstein.yaml`:

```yaml
# eu_residency_profile.yaml
profile: eu-residency
adapter: ollama
ollama:
  base_url: http://10.0.0.5:11434     # or http://vllm.internal:8000/v1
  eu_residency: true                  # turns the guard on for ALL models
data_residency:
  allowed_regions: [eu-west, eu-central]
  enforce_strict: true                # any region drift = abort
lineage:
  customer_signing_enabled: true
  kms_adapter: env                    # or 'file' / 'hsm' (see note below)
  kms_adapter_env_var: LINEAGE_SIGNING_KEY
  regulatory_class_default: production_detection_rule
network_policy:
  profile: airgap                     # implicit deny-all egress
```

The combination matters:

- `ollama.eu_residency: true` -- adapter-level guard on **every** model,
  not just `deepseek-v4-*`.
- `data_residency.enforce_strict: true` -- region drift halts the run
  rather than warning.
- `network_policy.profile: airgap` -- the runtime socket guard
  blocks any unintended outbound dial.
- `kms_adapter: hsm` is only viable when a customer-provided
  `HSMKMSAdapter` subclass (PKCS#11 / Cloud-KMS) is on the classpath.
  Without one, the orchestrator aborts at config-load time. For
  non-production smoke tests, set `BERNSTEIN_ALLOW_HSM_STUB=1` to opt
  in to the documentation stub explicitly. See
  [regulatory-lineage.md](regulatory-lineage.md) for the integration
  shape.

### 3. Verify the deployment with `bernstein doctor airgap`

Before processing any production traffic, run the air-gap doctor:

```bash
$ bernstein --profile airgap doctor airgap
```

Expected output (every row PASS):

```
Air-gap doctor: PASSED

Status  Check                              Detail
PASS    airgap profile active              BERNSTEIN_PROFILE_MODE=airgap
PASS    network policy deny-all            explicit deny-all (none)
PASS    runtime socket guard active        socket.socket.connect is patched
PASS    policy blocks declared endpoints   N declared endpoint(s) all blocked
PASS    MCP catalog all-off                no user MCP config
PASS    memo store on local disk           no shared cache; airgap pins memo to <path>
PASS    audit chain HMAC valid             N entries verified, chain intact
PASS    no external hostnames in runtime   no public endpoint references found
```

If any row reads FAIL, the suggested fix in the report is the
canonical remediation. The doctor exits non-zero on any FAIL row, so
wiring it into a deploy-time CI gate is one line.

### 4. Spot-check the residency guard with a deliberate failure

Compliance teams should occasionally probe the guard. Set the base
URL to a public hosted API (`https://api.deepseek.com`) and confirm
the spawn refuses:

```bash
$ OLLAMA_API_BASE=https://api.deepseek.com bernstein run \
    --adapter ollama --model deepseek-v4-flash --task probe-residency
RuntimeError: RESIDENCY_VIOLATION: model 'deepseek-v4-flash' requires a
self-hosted endpoint under the eu-residency profile, got
'https://api.deepseek.com'. Set OLLAMA_API_BASE / OLLAMA_HOST to a
self-hosted (e.g. vLLM, Ollama on a private/EU node) endpoint and retry.
```

The exit code is non-zero. Wire this scenario into your weekly
compliance smoke pipeline -- a passing guard is observable evidence
for the auditor.

## Expected log lines

A well-configured EU-residency run emits (one per line) the following
in `.sdd/audit/<YYYY-MM-DD>.jsonl` (the log is daily-rotated):

```
event=adapter.spawn adapter=ollama model=deepseek-v4-flash base_url=http://10.0.0.5:11434
event=residency.gate.passed model=deepseek-v4-flash host=10.0.0.5 verdict=self_hosted
event=lineage.signed regulatory_class=production_detection_rule kms=env
event=network.policy.allow host=10.0.0.5 port=11434 source=adapter:ollama
```

A misconfigured run (or an attacker-flipped base URL) emits:

```
event=residency.gate.violation model=deepseek-v4-flash base_url=https://api.deepseek.com
event=adapter.spawn.aborted reason=RESIDENCY_VIOLATION
```

The audit chain HMAC covers every line so a tampered log is
detectable via `bernstein audit verify` and
`bernstein doctor airgap`.

## What to hand the auditor

Three artefacts settle the Article-12 evidence story:

1. **`.sdd/audit/<YYYY-MM-DD>.jsonl`** -- the residency-gate decisions
   for the audit window (daily-rotated), with the HMAC chain intact.
2. **`MANIFEST.customer.json`** in the deployed wheelhouse -- the
   customer countersignature plus the org cosign signature, proving
   the running code was both vendor-signed AND customer-signed.
3. **`bernstein doctor airgap --json`** snapshot taken at audit
   start AND audit end -- proves the network policy and runtime
   socket guard stayed deny-all throughout.

## Common operator mistakes

| Symptom                                   | Likely cause                                                      |
|-------------------------------------------|-------------------------------------------------------------------|
| `RESIDENCY_VIOLATION` on a private host   | Host on a public IP without `*.internal` / `*.local` suffix       |
| `RESIDENCY_VIOLATION` on `172.32.5.5`     | Outside the RFC-1918 `172.16.0.0/12` band                         |
| `signature unverified` on customer sig    | `.bernstein/trust/customer-keys/` empty or missing the right key  |
| Doctor reports `MCP catalog all-off=FAIL` | Residual config from a previous non-airgap session; remove it     |

## See also

- [`docs/compliance/regulatory-lineage.md`](./regulatory-lineage.md)
  -- regulatory_class taxonomy and lineage v2 schema.
- [`scripts/build_airgap_wheelhouse.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/scripts/build_airgap_wheelhouse.py)
  -- offline wheel bundle builder.
- [`bernstein wheelhouse countersign --help`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/cli/commands/wheelhouse_cmd.py)
  -- customer-side countersignature CLI.
