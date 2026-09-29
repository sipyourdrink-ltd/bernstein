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
bernstein -g "fix the failing test" --adapter clm
```

For Ollama specifically, the adapter auto-discovers the endpoint from
`OLLAMA_API_BASE` (defaults to `http://localhost:11434`). No token is
required by default.

---

## Certifying an endpoint

`bernstein endpoints certify` qualifies any OpenAI-compatible base URL
against the conformance suite and writes a signed certification record to
disk.  The record is the claim: "this endpoint behaved contract-correctly
at qualification time."

```bash
bernstein endpoints certify \
  --base-url http://my-nim-host:8000/v1 \
  --token    "$CLM_TOKEN" \
  --model    "meta-llama/Llama-3-8B-Instruct"
```

The command:

1. Runs the conformance probe suite against the target URL (hermetic
   — no external network calls beyond the target itself).
2. Collects the pass/fail result for each probe.
3. Writes a signed certification record to
   `.sdd/certs/endpoints/<fingerprint>.json`, where `fingerprint` is the
   SHA-256 of `(base_url, model, timestamp)`.
4. Anchors the record in the HMAC audit chain as an
   `endpoint.certification` event (requires `--audit` or
   `BERNSTEIN_AUDIT=1`).

### Options

| Flag | Default | Description |
|---|---|---|
| `--base-url` | — | **Required.** Base URL of the OpenAI-compatible server (without trailing `/`). |
| `--token` | `""` | Bearer token forwarded as `Authorization: Bearer …`. Pass an empty string for servers that require no auth. |
| `--model` | — | **Required.** Model id to use for conformance probes. |
| `--out` | `.sdd/certs/endpoints/` | Directory to write the certification record. |
| `--strict` | `false` | Fail if any optional probe fails (not just required ones). |
| `--timeout` | `30` | Per-probe HTTP timeout in seconds. |

### Example output

```
bernstein endpoints certify \
  --base-url http://localhost:11434/v1 \
  --token    "" \
  --model    "llama3"

✔  probe: GET /v1/models               200 OK
✔  probe: POST /v1/chat/completions    200 OK (non-streaming)
✔  probe: POST /v1/chat/completions    200 OK (streaming, SSE)
✔  probe: tool_calls round-trip        present in response
✔  probe: finish_reason present        stop
✔  probe: role=assistant present       true

Certification written to .sdd/certs/endpoints/a3f8...c21.json
Audit chain anchor:   endpoint.certification  sha256=a3f8...c21
```

---

## Verifying a certification record offline

```bash
bernstein endpoints verify \
  --cert .sdd/certs/endpoints/a3f8...c21.json
```

`verify` re-reads the record, re-checks the Ed25519 signature against the
install identity, and confirms the audit-chain anchor (when `--audit-dir`
is provided).  No network call to the endpoint is made — the record is
self-contained.

```
bernstein endpoints verify \
  --cert .sdd/certs/endpoints/a3f8...c21.json \
  --audit-dir .sdd/audit/

✔  signature valid   (install key fp: ed25519/abc123)
✔  audit anchor      endpoint.certification @ 2026-07-24T10:31:00Z
✔  probes passed     6/6 required, 0 optional failures
   base_url          http://localhost:11434/v1
   model             llama3
   certified_at      2026-07-24T10:31:00Z
```

---

## Certification record schema

The `.sdd/certs/endpoints/<fp>.json` file is a signed JSON object:

```jsonc
{
  "schema":        "bernstein.endpoint.certification.v1",
  "base_url":      "http://localhost:11434/v1",
  "model":         "llama3",
  "certified_at":  "2026-07-24T10:31:00Z",
  "probes": [
    { "id": "models_list",          "required": true,  "passed": true },
    { "id": "chat_completions",     "required": true,  "passed": true },
    { "id": "chat_streaming",       "required": true,  "passed": true },
    { "id": "tool_calls",           "required": false, "passed": true },
    { "id": "finish_reason",        "required": true,  "passed": true },
    { "id": "assistant_role",       "required": true,  "passed": true }
  ],
  "passed":        true,
  "install_key_fp": "ed25519/abc123...",
  "signature":      "<detached JWS, RFC 7515 §A.5>"
}
```

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
  - name: local-llama
    base_url: http://localhost:11434/v1
    token: ""
    model: llama3
    cert: .sdd/certs/endpoints/a3f8...c21.json   # written by `bernstein endpoints certify`

roles:
  lint:
    adapter: clm
    endpoint: local-llama    # routes lint tasks to the local endpoint
  test-writer:
    adapter: clm
    endpoint: local-llama
```

Bernstein validates the cert at config-load time and refuses to dispatch
merge-critical roles to an uncertified endpoint.

---

## Troubleshooting

**"CLM adapter requires CLM_ENDPOINT, CLM_TOKEN, CLM_MODEL to be set"**
— Export the three required env vars before calling `bernstein`.

**Probe `chat_streaming` fails against llama.cpp**
— Older `llama-server` builds (< b3000) do not stream SSE correctly.
Upgrade to a current build or pass `--no-stream` to the adapter.

**Probe `tool_calls` fails (optional)**
— Not all models support tool-calling. This probe is optional and does
not block certification. Bernstein will refuse tool-call tasks against
the endpoint at runtime.

**Offline verify fails with "signature mismatch"**
— The cert was written by a different Bernstein install.  Re-certify on
the current machine, or export your install key and pass it via
`--install-key <pem>` to `bernstein endpoints verify`.

---

## See also

- [`docs/adapters/clm.md`](../adapters/clm.md) — CLM adapter configuration and mTLS guide
- [`docs/reference/local-endpoints.md`](../reference/local-endpoints.md) — local endpoint profiles
- [`docs/security/audit-log.md`](../security/audit-log.md) — HMAC audit chain
- [`examples/local-fleet/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/local-fleet) — multi-model local fleet example
