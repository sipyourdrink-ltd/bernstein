# Local endpoint profiles and certification

Local runtimes (ollama, LM Studio, MLX-based servers) expose competent
coding models behind OpenAI-compatible endpoints. Bernstein routes
low-stakes roles to that tier through named `local_endpoints` profiles and
gates merge-critical roles on a signed certification receipt.

## Endpoint profiles

Declare each local endpoint once and reference it per role:

```yaml
local_endpoints:
  workhorse:
    base_url: http://127.0.0.1:11434/v1   # any OpenAI-compatible endpoint
    model: qwen2.5-coder:7b-instruct-q4_K_M
    engine: ollama                         # free-form label, recorded in the receipt
    timeout: 120                           # request timeout in seconds
    # api_key_env: LOCAL_LLM_API_KEY       # NAME of an env var, never a key

role_model_policy:
  manager:                                 # merge-critical: stays on an API model
    model: gpt-5
    api_key_env: OPENAI_API_KEY
  linter:       { endpoint: workhorse }
  test_writer:  { endpoint: workhorse }
  triage:       { endpoint: workhorse }
  doc_sweeper:  { endpoint: workhorse }
```

A role entry with `endpoint:` inherits the profile's
`base_url`/`model`/`api_key_env`; setting those fields inline together with
`endpoint:` is a validation error, because the certification receipt is
keyed on the profile's exact `(base_url, model)` pair.

See [examples/local-fleet](https://github.com/sipyourdrink-ltd/bernstein/tree/main/examples/local-fleet)
for a complete mixed-fleet configuration.

## Role tiers

| Tier | Roles | Requirement |
|---|---|---|
| Local (low-stakes) | `linter`, `triage`, `doc_sweeper` | Reachability, chat completion, timeout behavior, context floor |
| Local (tooling) | `test_writer` | Low-stakes probes plus tool calling and patch format fidelity |
| Gated (merge-critical) | every other role, including `manager` and `default` | The full probe subset, certified by receipt |

The gate fails closed: a role Bernstein has never seen is gated. Assigning
an uncertified endpoint profile to a gated role fails config validation
with the exact `bernstein doctor` command to run.

## Certifying an endpoint

```bash
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --endpoint-engine ollama
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --role manager   # evaluate a gated role
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --json           # machine-readable verdicts
```

The doctor runs a fixed conformance subset with pinned prompts at
`temperature 0`:

| Probe | Rejection reasons |
|---|---|
| `reachability` | `unreachable`, `bad_models_payload` |
| `chat_completion` | `chat_failed`, `empty_completion`, `malformed_response` |
| `tool_calling` | `no_tool_call`, `tool_call_wrong_function`, `tool_call_malformed_arguments` |
| `patch_fidelity` | `patch_not_returned`, `patch_corrupted` |
| `timeout_behavior` | `timed_out` |
| `context_floor` | `context_rejected`, `empty_completion` |

The verdict is a pure function of the endpoint's responses: two runs that
observe the same responses produce byte-identical transcripts and per-role
verdicts. The capability check is the contract, not the engine name -- an
engine upgrade that drops tool calling is caught by re-running the doctor,
whatever the engine calls itself.

## The receipt

Certification is not a boolean in config. Each doctor run seals a receipt
under `.sdd/endpoints/certifications/<fingerprint>.json`:

- the probe transcript and per-role verdicts, bound into canonical JSON;
- an Ed25519 signature by the install's endpoint identity;
- an anchor in the `endpoint-certification` lineage spine run;
- a mirror event (`endpoint.certification`) in the HMAC audit chain.

Config validation gates merge-critical roles on the receipt verifying: a
hand-edited receipt fails its signature check exactly like a tampered chain
entry. `bernstein doctor --endpoint <url>` re-certifies in place; receipts
are keyed on the `(base_url, model)` pair, so switching models re-runs the
gate.

## Verified local configurations

Each row below is backed by a golden response transcript recorded from the
engine and replayed through the conformance evaluator in CI
(`tests/unit/endpoints/golden/`), so this table cannot drift from what the
certifier accepts.

| Engine | Version | Model | RAM budget | Certified tier |
|---|---|---|---|---|
| ollama | 0.9.6 | `qwen2.5-coder:7b-instruct-q4_K_M` | 8 GB | Full probe subset (all roles) |
| LM Studio | 0.3.16 | `llama-3.1-8b-instruct-q4_k_m` | 12 GB | Full probe subset (all roles) |
| mlx_lm.server (MLX) | 0.26.3 | `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` | 16 GB unified | Full probe subset (all roles) |

Everything else is best-effort by policy: run `bernstein doctor --endpoint`
against your own runtime and let the receipt decide. Quantized 7B-class
coder models are the practical floor for the tooling tier; smaller models
typically fail `patch_fidelity` first.
