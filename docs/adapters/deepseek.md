# DeepSeek V4 (Flash + Pro) - self-hosted

Bernstein routes DeepSeek's MIT-licensed V4 family through the
`ollama` adapter and an OpenAI-compatible HTTP endpoint. Both models
ship as MoE weights and are intended to run inside the operator's own
perimeter; the adapter refuses to spawn against a public DeepSeek API
when residency mode is active.

| Model | Architecture | Active params | Endpoint shape |
|-------|--------------|--------------:|----------------|
| `deepseek-v4-flash` | 284B / 13B-active MoE | 13B | Single-GPU Ollama (H100/A100) |
| `deepseek-v4-pro` | 1.6T / 49B-active MoE | 49B | vLLM tensor-parallel (multi-GPU) |

Both names round-trip through `model_config.model` and through the
adapter's `_MODEL_MAP` (see `src/bernstein/adapters/ollama.py`).
Because both endpoints expose the OpenAI-compatible `/v1/chat/completions`
surface, aider/litellm treats Ollama and vLLM interchangeably - the
only operator choice is whether to point `OLLAMA_API_BASE` at the
local Ollama daemon or at the vLLM tensor-parallel server.

---

## EU-residency guard

The DeepSeek V4 names are pinned in `_EU_RESIDENCY_MODELS` and trigger
the residency guard regardless of the `eu_residency=True` constructor
flag. When the guard fires, the adapter resolves the configured base
URL to a host and accepts only the following shapes:

| Shape | Examples |
|-------|----------|
| Loopback hostname | `localhost` |
| IPv4 loopback / private | `127.0.0.1`, `10.x.x.x`, `172.16-31.x.x`, `192.168.x.x` |
| IPv6 loopback / unique-local / link-local | `::1`, `fc00::/7`, `fe80::/10` |
| Internal-suffix FQDN | `*.internal`, `*.local`, `*.svc`, `*.cluster.local` |

Anything else fails with `RESIDENCY_VIOLATION`, naming both the
offending endpoint and the model that triggered the guard. Operators
who try to point the adapter at the public `deepseek.com` API see the
refusal at spawn time, before any prompt bytes leave the orchestrator.

### Octet-aware host check

Earlier residency checks used `host.startswith("10.")` style prefix
matching, which silently accepted attacker-controlled FQDNs that begin
with the same characters as a private range - `10.example.com`,
`192.168.evil.tld`, `172.20.foo.com`. The current implementation
parses the host through `ipaddress.ip_address` and falls back to the
explicit FQDN-suffix allowlist only when the host is not a literal
IP. The Hypothesis bug-hunt suite covers the `10.example.com`
rebinding bypass as a `xfail(strict=True)` invariant so a regression
trips the test before it reaches a release.

`0.0.0.0` is intentionally **not** on the allowlist: it is the IPv4
wildcard, not loopback, and would whitelist any interface the host
happens to bind.

---

## Configuration

The DeepSeek path uses the same `ollama` adapter knobs as any other
local model:

```python
from bernstein.adapters.ollama import OllamaAdapter

adapter = OllamaAdapter(
    base_url="http://10.0.0.5:11434",   # Ollama on a private node
    eu_residency=True,                   # belt-and-braces; the model
                                         # alone already pins the guard
)
```

Or via the standard env variables:

```bash
export OLLAMA_API_BASE=http://10.0.0.5:11434
export OLLAMA_HOST=http://10.0.0.5:11434
```

For `deepseek-v4-pro`, point `OLLAMA_API_BASE` at the vLLM `/v1`
endpoint instead - same env variable, same wire format:

```bash
python -m vllm.entrypoints.openai.api_server \
    --model deepseek-ai/deepseek-v4-pro \
    --tensor-parallel-size 8 \
    --host 10.0.0.5 \
    --port 8000

export OLLAMA_API_BASE=http://10.0.0.5:8000/v1
```

Aider then dispatches `--model ollama/deepseek-v4-pro` and litellm's
OpenAI-compatible path treats the vLLM endpoint exactly as it would a
local Ollama daemon.

### Pair with `DataResidencyController`

The endpoint guard refuses to *spawn* against a non-self-hosted host.
For the full Article-12 evidence story, combine it with
`bernstein.core.security.data_residency.DataResidencyController`:

```python
from bernstein.core.security.data_residency import (
    DataResidencyController,
    Region,
    ResidencyPolicy,
)

residency = DataResidencyController(node_region=Region.EU_WEST)
residency.set_policy(
    ResidencyPolicy(
        tenant_id="acme",
        allowed_regions=frozenset({Region.EU_WEST, Region.EU_CENTRAL}),
        primary_region=Region.EU_WEST,
        enforce_strict=True,
    )
)
```

Regions are members of the `Region` enum, and the allowed set is
per-tenant policy state rather than a controller constructor argument.

The two layers are orthogonal: the adapter guard pins the *endpoint*,
the controller pins the *region the workload may reach*.

---

## Model selection

| Bernstein tier | Native Ollama / vLLM model |
|----------------|---------------------------|
| `opus` | `deepseek-r1:70b` (default) |
| `deepseek-v4-flash` | `deepseek-v4-flash` |
| `deepseek-v4-pro` | `deepseek-v4-pro` |

Pass either the tier name or the native model id through
`model_config.model`. The DeepSeek V4 names short-circuit the
residency check on; mapping them to the public DeepSeek API would
silently violate the residency promise, so the adapter refuses
that path even when `eu_residency=False`.

---

## Wire format and audit

Aider drives the gateway via `--model ollama/<model>` plus the
standard `OPENAI_API_BASE` env, so the prompt and response shape match
every other OpenAI-compatible adapter. Bernstein's audit chain records
the prompt SHA and the model name; the lineage record carries the
endpoint host (already redacted of credentials) so an evaluator can
prove which infrastructure served the call.

The `network_policy` check still fires at spawn time so a misconfigured
allowlist refuses the connection before the subprocess starts -
residency guard and network policy are independent gates and both must
pass.

---

## Limitations

- OpenAI-compatible HTTP only. A non-OpenAI-shaped endpoint requires a
  separate adapter shim.
- One client cert per spawn when the upstream gateway requires mTLS;
  the [`clm` adapter](clm.md) covers the dedicated mTLS path.
- Per-chunk lineage is not in scope. Streaming responses are assembled
  and emitted to lineage as a single record.

## Related

- Source: `src/bernstein/adapters/ollama.py` (the DeepSeek V4 entries
  in `_MODEL_MAP` and the `_EU_RESIDENCY_MODELS` allowlist live there).
- [`ollama` adapter profile](ADAPTER_GUIDE.md#ollama-local-llms)
- [EU-residency customer setup](../compliance/eu-residency-customer-setup.md)
- [`DataResidencyController`](../security/security-hardening.md)
- [Compatibility matrix](compatibility.md)
