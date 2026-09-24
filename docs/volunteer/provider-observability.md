# Provider Observability in Volunteer Mode

## Overview

When a donor runs volunteer tasks using a provider-backed adapter (API-based or OAuth-authenticated), the provider observes API calls originating from the donor's machine. This document enumerates what providers observe at the API-call boundary, per adapter class, and establishes the recommended default posture: **local/self-hosted execution** to eliminate third-party observability entirely.

## Key Finding: Topology Does Not Change the Call

The network topology of the coordination layer — how tasks are discovered and claimed — does not change what a single donor's API call looks like to a provider. The call is made locally by the donor's own machine regardless of whether discovery is centralized, federated, or peer-to-peer.

**Provider-opacity is therefore an adapter/client concern, not a networking concern.** A decentralized transport does not address it. The question is: which adapter class does the donor use, and what does that adapter send over the network?

## Adapter Classes and Provider Observability

### CLI-Wrapping Adapters

**What they are:** Adapters that shell out to a locally-installed CLI tool (e.g., `claude`, `aider`, `codex`, `cursor`). Bernstein spawns the CLI process; the CLI itself makes the network calls to the provider.

**What the provider observes:**
- **Auth principal:** API key or OAuth token the CLI uses
- **Source IP:** Donor's public IP or VPN exit
- **Request shape:** Whatever the CLI tool sends (prompts, tool calls, streaming mode)
- **Client metadata:** User-Agent, SDK version strings, telemetry the CLI emits
- **Pacing:** Call timing and frequency patterns

**What distinguishes volunteer tasks from interactive use:** Nothing at the API boundary. The provider sees the same CLI tool making calls from the same key. The only observable difference is pacing — volunteer tasks may have longer or more frequent bursts if the orchestrator spawns multiple agents concurrently.

**Provider control:** The CLI tool's behavior (what headers it sends, what telemetry it emits) is determined by the tool's maintainer, not by Bernstein. Bernstein cannot inspect or modify the HTTP calls the CLI makes.

**Examples:** `claude`, `aider`, `codex`, `cursor`, `gemini`, `qwen`, `continue`, `cody`, `goose`, `plandex`

### Direct HTTP Adapters

**What they are:** Adapters that make HTTP requests directly from Bernstein's process using `httpx`, `requests`, or `urllib`.

**What the provider observes:**
- **Auth principal:** API key or OAuth token
- **Source IP:** Donor's public IP or VPN exit
- **Request shape:** JSON payloads Bernstein constructs
- **Client metadata:** User-Agent headers Bernstein sets (or defaults)
- **Pacing:** Call timing and frequency

**What distinguishes volunteer tasks from interactive use:** Potentially the User-Agent string if Bernstein sets one that identifies itself. Otherwise, nothing observable — the provider sees standard API requests from the donor's key.

**Provider control:** Bernstein controls the HTTP headers and payload. If Bernstein sets a User-Agent identifying itself, that is observable. If Bernstein uses a generic or omitted User-Agent, the call is indistinguishable from other API clients.

**Examples:** Some adapters in the fleet make direct HTTP calls (specific names omitted to avoid brittle enumeration — the implementation layer changes frequently).

### Local/Self-Hosted Adapters

**What they are:** Adapters that connect to local models (Ollama, llama.cpp, vLLM) or self-hosted OpenAI-compatible endpoints. No third-party provider involved.

**What the provider observes:** Nothing. No external network call is made. All execution is donor-local or self-hosted.

**Examples:** `local` tier adapters certified via `bernstein.core.endpoints`, any adapter pointed at `http://localhost:*` or a donor-controlled endpoint.

**This is the recommended default posture for volunteer mode.**

## Default Posture: Local-First

When no adapter is explicitly chosen and a certified local endpoint exists for the needed role, volunteer mode selects the local endpoint. This eliminates provider observability entirely.

If no local endpoint is available and no adapter was chosen, volunteer mode falls back to the donor's configured default adapter with a logged warning naming the trade-off:

```
No adapter chosen and no certified local endpoint for role=backend.
Falling back to configured default. Consider certifying a local endpoint
to minimize provider observability.
```

This ensures the donor makes an informed decision: they can configure a local endpoint (removing the provider from the loop) or explicitly choose a provider adapter (accepting the observability trade-off).

## What This Does NOT Do

- **Not an evasion layer.** The recommendation is "use provider-independent execution," not "hide volunteer tasks from providers."
- **Not a terms-of-service workaround.** If a provider's terms prohibit orchestrated use, the answer is to use a local/self-hosted model, not to disguise the calls.
- **Not credential isolation.** Credentials never leave the donor machine regardless of adapter class. This document addresses what the provider API endpoint observes, not credential handling.

## Security Considerations

1. **CLI-wrapping adapters are opaque to Bernstein.** The CLI tool's network behavior is not under Bernstein's control. Donors using CLI-wrapping adapters should verify the CLI tool's terms of service and privacy policy independently.

2. **Direct HTTP adapters expose User-Agent.** If Bernstein sets a User-Agent header identifying itself, that is observable to the provider. Donors concerned about this should use local/self-hosted adapters.

3. **Local endpoints eliminate provider observability.** A donor running tasks with a local model (Ollama, llama.cpp, self-hosted vLLM) makes no external network calls to LLM providers. The provider-observability question does not apply.

## References

- `:mod:`bernstein.core.endpoints` — local-model worker tier and certification
- `:mod:`bernstein.core.volunteer.adapter_selection` — selection logic implementing the local-first default
- `docs/volunteer/threat-model.md` — sandbox boundary and containment model
- `:func:`bernstein.core.volunteer.runner._validate_volunteer_auth_basis` — auth-basis gate for adapters

## Implementation

The selection logic lives in `:mod:`bernstein.core.volunteer.adapter_selection`. Tests proving the local-first default:

- `test_no_explicit_choice_and_a_certified_local_endpoint_available_selects_local`
- `test_an_explicit_provider_choice_is_honored_even_when_local_is_available`
- `test_no_explicit_choice_and_no_local_endpoint_available_falls_back_to_whatever_is_configured`
- `test_the_selection_function_never_reads_provider_credentials_to_decide`

See `tests/unit/volunteer/test_adapter_selection.py`.
