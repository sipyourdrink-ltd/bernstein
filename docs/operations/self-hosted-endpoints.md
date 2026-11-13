# Self-Hosted OpenAI-Compatible Endpoints

This page covers pointing a Bernstein adapter at any OpenAI-compatible
inference server, running the endpoint conformance suite to qualify it,
and reading the resulting signed certification record.

---

## Which adapter to use

| Scenario | Adapter |
|---|---|
| Ollama (local) | `ollama` — `pip install aider-chat`, set `OLLAMA_API_BASE` |
| llama.cpp server | `clm` — set `CLM_ENDPOINT` / `CLM_TOKEN` / `CLM_MODEL` |
| vLLM | `clm` — same env-var bundle |
| TGI (text-generation-inference) | `clm` — same env-var bundle |
| NVIDIA NIM | `clm` — same env-var bundle (opt-in mTLS via `CLM_CERT_FILE` / `CLM_KEY_FILE` / `CLM_CA_FILE`) |
| LM Studio | `ollama` or `clm` — LM Studio exposes the `/v1/` path on `localhost:1234` by default |
| Qwen-Code local | `qwen` — `npm install -g @qwen-code/qwen-code`, set endpoint via `OPENAI_BASE_URL` |
| Any other server speaking the OpenAI wire protocol | `clm` with the appropriate `CLM_ENDPOINT` |

All of the above expose the same `/v1/chat/completions` + `/v1/models`
wire surface. Bernstein's conformance suite qualifies them identically
regardless of the underlying runtime.

---

## Minimal configuration (CLM adapter)

```bash
export CLM_ENDPOINT="http://my-nim-host:8000/v1"
export CLM_TOKEN="<scoped-jwt-or-dummy>"
export CLM_MODEL="meta-llama/Llama-3-8B-Instruct"
```

Then run as normal:

```bash
bernstein run --goal "fix the failing test" --cli clm
```

For Ollama specifically, the adapter auto-discovers the endpoint from
`OLLAMA_API_BASE` (defaults to `http://localhost:11434`). No token is
required by default.

---

## Certifying an endpoint

`bernstein endpoints certify` runs conformance probes against the target
and writes an Ed25519-signed receipt with the probe transcript and per-role verdicts.

```bash
bernstein endpoints certify \
  --base-url http://my-nim-host:8000/v1 \
  --api-key-env CLM_TOKEN \
  --model "meta-llama/Llama-3-8B-Instruct"
```

The receipt is stored at `.sdd/endpoints/certifications/<fingerprint>.json`.
The fingerprint is derived from the normalized base URL and the model.
The command records an anchor in the certification lineage spine and
mirrors the seal in the HMAC audit chain.

### Options

| Flag | Default | Description |
|---|---|---|
| `--base-url` | — | **Required.** Base URL of the OpenAI-compatible server. |
| `--api-key-env` | — | Name of the variable holding the bearer token. Omit for endpoints that need no authentication. |
| `--model` | First model from `/models` | Model to use for the probes. |
| `--engine` | Empty | Runtime label stored in the receipt, such as `ollama` or `nim`. |
| `--timeout` | `60` | Per-probe response budget in seconds. |
| `--role` | Low-stakes local roles | Role to evaluate. Repeat for multiple roles. |
| `--json` | Off | Print results as JSON. |

For an endpoint that requires no token:

```bash
bernstein endpoints certify \
  --base-url http://localhost:11434/v1 \
  --model llama3
```

---

## Verifying a certification record offline

Run verification from the workspace that holds the receipt and lineage:

```bash
bernstein endpoints verify \
  --base-url http://localhost:11434/v1 \
  --model llama3
```

Verification looks up the stored receipt by base URL and model. It checks
the endpoint identity, the Ed25519 signature and the certification spine
anchor using the audit key. It makes no request to the endpoint.

The receipt contains the signed endpoint identity, engine label, suite
version, probe transcript, role verdicts and timestamp. It also carries
the signer's public key, signature and lineage entry hash.

---

## Exercised endpoint families

The conformance suite has been run against all of the following.
Any server speaking the same OpenAI wire surface qualifies the same way.

| Family | Notes |
|---|---|
| **vLLM** | `vllm serve <model>` — `/v1/` on port 8000 by default. Tool-calls surface available from v0.4+. |
| **llama.cpp server** | `llama-server -m model.gguf --port 8080`. Set `CLM_ENDPOINT=http://localhost:8080/v1`. |
| **TGI** | Hugging Face text-generation-inference v2+. Needs `--enable-api-keys` and `HUGGING_FACE_HUB_TOKEN` if using gated models. |
| **NVIDIA NIM** | NIM containers expose `/v1/` directly. Opt-in mTLS via `CLM_CERT_FILE` / `CLM_KEY_FILE` / `CLM_CA_FILE`. |
| **LM Studio** | Local server on `localhost:1234`. Use `clm` adapter with `CLM_ENDPOINT=http://localhost:1234/v1` and `CLM_TOKEN=lm-studio`. |
| **Ollama** | `OLLAMA_API_BASE=http://localhost:11434`. Use the `ollama` adapter or `clm` pointing at the `/v1/` path. |

---

## Using a certified endpoint in `bernstein.yaml`

```yaml
local_endpoints:
  local-llama:
    base_url: http://localhost:11434/v1
    model: llama3

role_model_policy:
  linter:
    endpoint: local-llama
  test_writer:
    endpoint: local-llama
```

Merge-critical roles require a receipt that certifies the assigned role.
Pass `--role ROLE` when certifying an endpoint for one of those roles.

---

## Troubleshooting

**"CLM adapter requires CLM_ENDPOINT, CLM_TOKEN, CLM_MODEL to be set"**
— Export the three required env vars before calling `bernstein`.

**The tool-calling probe fails**
— Check the per-role verdicts. Tool calling is required for merge-critical
roles and `test_writer`. Choose an endpoint and model that support it for
those roles.

**Offline verification reports a signature or lineage error**
— Use the original receipt and certification spine with the corresponding
audit key. If those records are unavailable and the endpoint is reachable,
run certification again in the current workspace.

---

## See also

- [`docs/adapters/clm.md`](../adapters/clm.md) — CLM adapter configuration and mTLS guide
- [`docs/reference/local-endpoints.md`](../reference/local-endpoints.md) — local endpoint profiles
- [`docs/security/audit-log.md`](../security/audit-log.md) — HMAC audit chain
- [`examples/local-fleet/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/local-fleet) — multi-model local fleet example
