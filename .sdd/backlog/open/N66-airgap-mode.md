# N66 — Airgap Mode

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Classified and air-gapped environments cannot use Bernstein because it assumes external network access to cloud AI providers, making it unusable in defense and high-security contexts.

## Solution
- Implement `bernstein run --airgap` flag that disables all external network calls
- In airgap mode, require a local model endpoint (Ollama, vLLM, or compatible)
- Adapter auto-detects local endpoint via well-known ports and environment variables
- Fail fast with a clear error if no local model is available
- Block any attempt to reach external URLs during airgap execution

## Acceptance
- [ ] `bernstein run --airgap` disables all external network calls
- [ ] Adapter auto-detects local model endpoints (Ollama, vLLM)
- [ ] Execution works end-to-end with a local model
- [ ] Clear error message if no local model endpoint is found
- [ ] Any external network attempt during airgap mode is blocked and logged
