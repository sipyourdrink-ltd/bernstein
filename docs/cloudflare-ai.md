# Workers AI Provider

**Module:** `bernstein.core.routing.cloudflare_ai`
**Class:** `WorkersAIProvider`

Cloudflare Workers AI provides free-tier LLM models that Bernstein can use for task decomposition, planning, manager decisions, and structured output generation. This lets you run the orchestrator's internal LLM calls at zero cost.

---

## Available models

All models listed below are free on Workers AI:

| Model | Context | Speed | Best for |
|-------|---------|-------|----------|
| `@cf/meta/llama-3.1-70b-instruct` | 131,072 | Medium | Planning, decomposition (default) |
| `@cf/meta/llama-3.1-8b-instruct` | 131,072 | Fast | Simple classification, routing |
| `@cf/mistral/mistral-7b-instruct-v0.2` | 32,768 | Fast | Quick completions |
| `@cf/google/gemma-7b-it` | 8,192 | Fast | Short prompts, simple tasks |
| `@cf/qwen/qwen1.5-14b-chat` | 32,768 | Medium | Multilingual tasks |

!!! tip "Zero-cost planning"
    Use Workers AI as your `internal_llm_provider` in bernstein.yaml to eliminate LLM costs for orchestrator-internal calls (task decomposition, priority assignment, plan optimization). Agent execution still uses your configured CLI adapter.

---

## Configuration

`WorkersAIConfig` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `account_id` | `str` | (required) | Cloudflare account ID |
| `api_token` | `str` | (required) | API token with Workers AI: Run permission |
| `model` | `str` | `"@cf/meta/llama-3.1-70b-instruct"` | Model identifier |
| `max_tokens` | `int` | `4096` | Maximum output tokens |
| `temperature` | `float` | `0.3` | Sampling temperature |
| `timeout_seconds` | `int` | `60` | HTTP request timeout |

---

## Usage

### Text completion

```python
from bernstein.core.routing.cloudflare_ai import WorkersAIConfig, WorkersAIProvider

provider = WorkersAIProvider(WorkersAIConfig(
    account_id="abc123",
    api_token="cf_token_...",
))

response = await provider.complete(
    "Decompose this task into 3 subtasks: Add authentication to the API",
    system="You are a senior engineering manager planning work for a team.",
)

print(response.text)
print(response.model)          # "@cf/meta/llama-3.1-70b-instruct"
print(response.input_tokens)   # token count from API
print(response.output_tokens)
print(response.is_free)        # True for free-tier models
```

### Structured JSON output

```python
schema = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "role": {"type": "string"},
                    "priority": {"type": "integer"},
                },
            },
        },
    },
}

result = await provider.structured(
    "Decompose: Add OAuth2 login to the web app",
    schema=schema,
    system="Return a task decomposition as JSON.",
)
# result is a parsed dict matching the schema
print(result["subtasks"])
```

!!! note "JSON parsing"
    The `structured()` method automatically strips markdown code fences from model output before parsing. If the model returns invalid JSON, a `json.JSONDecodeError` is raised.

### Cost estimation

```python
cost = provider.estimate_cost(input_tokens=1000, output_tokens=500)
print(f"${cost:.6f}")  # $0.000000 for free models

# List all available models with metadata
models = WorkersAIProvider.available_models()
for name, info in models.items():
    print(f"{name}: free={info['free']}, context={info['context']}")
```

---

## Response type

`WorkersAIResponse` fields:

| Field | Type | Description |
|-------|------|-------------|
| `text` | `str` | Generated text |
| `model` | `str` | Model identifier used |
| `input_tokens` | `int` | Input token count (from API usage) |
| `output_tokens` | `int` | Output token count |
| `is_free` | `bool` | Whether this model is on the free tier |

---

## Integration with bernstein.yaml

To use Workers AI as the internal scheduler LLM:

```yaml
# bernstein.yaml
internal_llm_provider: cloudflare_ai
internal_llm_model: "@cf/meta/llama-3.1-70b-instruct"
```

This routes all orchestrator-internal LLM calls (task decomposition, priority assignment) through Workers AI while agents still use your configured CLI adapter (Claude, Codex, Gemini, etc.).

---

## Cost comparison

| Provider | Model | Input cost/1M tokens | Output cost/1M tokens | Planning cost for 50-task run |
|----------|-------|---------------------|----------------------|-------------------------------|
| Workers AI | Llama 3.1 70B | $0.00 | $0.00 | $0.00 |
| Workers AI | Llama 3.1 8B | $0.00 | $0.00 | $0.00 |
| Anthropic | Claude Haiku | ~$0.25 | ~$1.25 | ~$0.50 |
| Anthropic | Claude Sonnet | ~$3.00 | ~$15.00 | ~$6.00 |
| OpenAI | GPT-4o-mini | ~$0.15 | ~$0.60 | ~$0.30 |

!!! tip "Hybrid approach"
    Use Workers AI for planning/decomposition (free) and Claude/Codex/Gemini for actual code generation (paid but high quality). This eliminates orchestrator overhead costs entirely.
