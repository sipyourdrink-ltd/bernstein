# Adapter auth basis and volunteer-mode status

## Auth basis

Every adapter contract (`tests/contract/contracts/*.yaml`) declares `auth.basis` (`AuthBasis` in `src/bernstein/adapters/_contract.py`):

| Value | Meaning | Examples |
|---|---|---|
| `api_key` | API-key auth (env var like `ANTHROPIC_API_KEY`) | `claude`, `codex`, `gemini`, `aider`, `opencode`, `pydantic_ai` |
| `local` | No remote auth — local model / in-process | `mock`, `generic` |
| `subscription_oauth` | OAuth / subscription-based | `agy` (Google consumer lane), `copilot` (GitHub auth) |
| `unknown` | Not declared in contract | `computer_use`, `integration-mock` |

## Volunteer-mode rules (from `bernstein.core.volunteer`)

Volunteer donor budgets (`VolunteerBudget`) enforce adapter selection through `filter_local_profiles` and `refuses_claim`:

- `local_only=True` (budget requires local-only adapter): only adapters with `local_models=True` are permitted (`opencode`, `pydantic_ai`). `api_key` and `subscription_oauth` adapters refused.
- `local_ok=True` (manifest allows non-local): `api_key` adapters permitted (`claude`, `codex`, `gemini`, etc.), `subscription_oauth` permitted if the provider allows it, `local` always permitted.
- `subscription_oauth` adapters (`agy`, `copilot`) refused when `local_only=True`; permitted under `local_ok=True` only if the donor budget allows non-local and the adapter profile has `local_models=False` (standard case).
- `unknown` auth basis treated conservatively — permitted only when `local_ok=True` and adapter profile is explicitly declared.

## Adapter table (contracts with auth basis)

| Adapter | Auth basis | Volunteer mode allowed? | Notes |
|---|---|---|---|
| `claude` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `ANTHROPIC_API_KEY` |
| `codex` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `OPENAI_API_KEY` |
| `gemini` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `GEMINI_API_KEY` |
| `antigravity` | `api_key` | Yes (`local_ok`) / No (`local_only`) | Enterprise / API-key lane |
| `aider` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `OPENAI_API_KEY` / multi-provider |
| `amp` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `AMP_API_KEY` |
| `copilot` | `subscription_oauth` | Yes (`local_ok`) / No (`local_only`) | GitHub Copilot auth |
| `q_dev` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `AMAZON_Q_TOKEN` |
| `qwen` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `DASHSCOPE_API_KEY` |
| `ollama` | — | — | No contract; `local_models` via Aider + local endpoint |
| `gptme` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `OPENAI_API_KEY` |
| `crush` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `ANTHROPIC_API_KEY` (Charm) |
| `droid` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `FACTORY_API_KEY` / multi-provider |
| `forge` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `FORGE_API_KEY` |
| `goose` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `ANTHROPIC_API_KEY` |
| `muse` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `META_API_KEY` |
| `continue` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `CONTINUE_API_KEY` |
| `opencode` | `api_key` | **Yes** (both if `local_ok` / `local_only`) | `OPENROUTER_API_KEY`; `local_models=True` |
| `pydantic_ai` | `api_key` | **Yes** (both if `local_ok` / `local_only`) | `local_models=True`; multi-provider via `clai` |
| `kimchi` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `KIMCHI_API_KEY` |
| `kimi` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `MOONSHOT_API_KEY` |
| `plandex` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `PLANDEX_API_KEY` |
| `python_runtime` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `OPENAI_API_KEY` |
| `aichat` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `OPENAI_API_KEY` / multi-provider |
| `devin_terminal` | `api_key` | Yes (`local_ok`) / No (`local_only`) | `DEVIN_API_KEY` / `WINDSURF_API_KEY` |
| `agy` | `subscription_oauth` | Yes (`local_ok`) / No (`local_only`) | Consumer lane; Google OAuth |
| `computer_use` | `unknown` | Yes (`local_ok`) / No (`local_only`) | Third-party agent; no contract auth basis |
| `integration-mock` | `unknown` | Yes (`local_ok`) / No (`local_only`) | Test stub; no contract auth basis |
| `mock` | — | **Yes** (both) | No contract; zero-key, no network |
| `generic` | — | **Yes** (both if profile declares `local_models`) | Depends on configured CLI |
| `clm` | — | Yes (`local_ok`) / No (`local_only`) | No contract; customer gateway mTLS |
| `openai_agents` | — | Yes (`local_ok`) / No (`local_only`) | No contract; SDK v2; `OPENAI_API_KEY` |
| `self-hosted-endpoints` | — | Yes (`local_ok`) / No (`local_only` if uncertified) | Requires `bernstein endpoints certify` |