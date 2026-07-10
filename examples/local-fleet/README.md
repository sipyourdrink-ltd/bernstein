# Mixed fleet: API planner plus certified local workers

This example routes low-stakes roles (lint, test-writing, triage, docstring
sweeps) to a local OpenAI-compatible endpoint while the merge-critical
manager stays on an API model.

## 1. Start a local runtime

Any OpenAI-compatible endpoint works. For example with ollama:

```bash
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
ollama serve
```

LM Studio and MLX servers work the same way; set
`BERNSTEIN_LOCAL_LLM_BASE_URL` / `BERNSTEIN_LOCAL_LLM_MODEL` to override the
defaults in `bernstein.yaml`.

## 2. Certify the endpoint

```bash
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --endpoint-engine ollama
```

The doctor runs a fixed conformance subset -- reachability, chat completion,
tool calling, patch format fidelity, timeout behavior, context floor -- and
prints a per-role certify/reject verdict with machine reason codes. The
result is a signed receipt anchored to the audit chain, not a boolean in
config: verify it any time with the same command, and inspect
`.sdd/endpoints/certifications/`.

The low-stakes roles in this example run without a receipt (best-effort by
policy). If you later route a merge-critical role (for example `manager`)
to the profile, config validation refuses to run until a receipt certifies
that role:

```bash
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --role manager
```

## 3. Run the fleet

```bash
export OPENAI_API_KEY=...   # the manager's endpoint credential
bernstein run
```

The manager plans and reviews on the API endpoint; the four local workers
pick up lint, test, triage, and docstring tasks against the local profile.

Verified engine/model/RAM combinations are listed in
[docs/reference/local-endpoints.md](../../docs/reference/local-endpoints.md).
