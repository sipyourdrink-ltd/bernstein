# Model Routing Policy — CISO-Level Provider Constraints

Model Policy is a CISO-level control system for managing where code and data can be sent in Bernstein. It provides transparent, auditable constraints on LLM provider selection — allowing enterprises to enforce compliance, data residency, and security requirements.

## Why Model Policy

Bernstein's intelligent router selects providers dynamically based on task complexity, cost, and health. But enterprises have hard constraints:

- **"Code never leaves Anthropic"** — proprietary code stays in-house
- **"No cloud APIs"** — local models only (Ollama, etc.)
- **"SOC2 certified providers only"** — compliance requirement
- **"Free tier only"** — cost control
- **"Preferred provider if available"** — optimize for cost OR performance

Model Policy enforces these constraints **before** any routing algorithm runs, ensuring that denied providers are never offered to the router.

## Configuration

### In `bernstein.yaml`

```yaml
model_policy:
  # Explicit allow-list: only these providers are available
  # If set, ONLY these providers will be used
  allowed_providers:
    - anthropic
    - ollama

  # Explicit deny-list: these providers are never used
  # Ignored if allowed_providers is set
  denied_providers:
    - openai
    - cohere

  # Preferred provider (must be allowed by the policy)
  # Router will use this provider if available, otherwise fallback
  prefer: anthropic
```

### In separate `model_policy.yaml`

You can also define the policy in `.sdd/config/model_policy.yaml`:

```yaml
allowed_providers:
  - anthropic
  - ollama

prefer: anthropic
```

## Policy Semantics

### Allow-list Mode
If `allowed_providers` is set, **only those providers are available**. The deny-list is ignored.

```yaml
model_policy:
  allowed_providers: [anthropic]
  # Result: Only anthropic provider is used, no other providers available
```

### Deny-list Mode
If `allowed_providers` is **not** set, the deny-list blocks specific providers.

```yaml
model_policy:
  denied_providers: [openai, cohere]
  # Result: All providers except openai and cohere are available
```

### No Policy
If neither is set, all registered providers are available (default).

```yaml
model_policy: {}
# Result: All providers available, router selects based on cost/health
```

## Integration with Routing

The policy filter sits **before** routing decisions:

1. **Provider Registration** — All providers are registered with the router
2. **Policy Filter** — Policy removes denied providers from the action space
3. **Routing Decision** — Router selects from the remaining (allowed) providers
4. **Execution** — Selected provider handles the task

Example:

```python
# 10 providers registered
# Policy denies openai, cohere
# Router sees 8 allowed providers
# Router selects best from those 8

router = TierAwareRouter()
router.register_provider(ProviderConfig(...))  # ... 10 providers

# Apply policy
policy = ModelPolicy(
    denied_providers=["openai", "cohere"],
    prefer="anthropic"
)
router.state.model_policy = policy

# Router now respects the policy
available = router.get_available_providers()  # Returns 8 (not 10)
decision = router.select_provider_for_task(task)  # Selected from 8
```

## Validation

Use `bernstein config validate` to check policy consistency:

```bash
$ bernstein config validate

✓ Configuration is valid

┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Provider            ┃ Tier    ┃ Status    ┃ Policy Allowed ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ anthropic           │ standard│ healthy   │ yes            │
│ ollama              │ free    │ healthy   │ yes            │
│ openai              │ premium │ healthy   │ no             │
└─────────────────────┴─────────┴───────────┴────────────────┘
```

Validation checks:

- **Policy syntax**: No conflicts (allow/deny overlap)
- **Preferred provider**: Must be allowed (not denied)
- **Provider availability**: At least one provider per tier is available after policy
- **Provider registration**: All referenced providers are registered

Issues trigger a non-zero exit code:

```bash
$ bernstein config validate
Configuration issues found:
  • Preferred provider 'openai' is in deny list
  • No available providers for tier 'premium' after policy constraints

$ echo $?
1
```

## Examples

### Example 1: Enterprise — Code Never Leaves Anthropic

```yaml
model_policy:
  allowed_providers:
    - anthropic
  prefer: anthropic
```

**Effect**: All tasks use Anthropic. No fallback to other providers.

### Example 2: Cost Control — Free Tier Only

```yaml
model_policy:
  allowed_providers:
    - ollama
  prefer: ollama
```

**Effect**: Only local Ollama models. No cloud API calls at all.

### Example 3: Compliance — Block Specific Providers

```yaml
model_policy:
  denied_providers:
    - openai         # Uses data for training
    - cohere         # Not SOC2 certified
  prefer: anthropic
```

**Effect**: Use any provider except OpenAI/Cohere. Prefer Anthropic if available.

### Example 4: Local Only — No Cloud APIs

```yaml
model_policy:
  allowed_providers:
    - ollama
    - llama-cpp
```

**Effect**: Only local models. No cloud API calls at all.

## API

### Python API

```python
from bernstein.core.routing.router import ModelPolicy, TierAwareRouter

# Create policy
policy = ModelPolicy(
    allowed_providers=["anthropic"],
    prefer="anthropic"
)

# Validate
issues = policy.validate()
if issues:
    for issue in issues:
        print(f"Issue: {issue}")

# Apply to router
router = TierAwareRouter()
router.state.model_policy = policy
router.policy_filter = PolicyFilter(policy=policy)

# Validate router configuration
router_issues = router.validate_policy()
```

### YAML API

Load from file:

```python
from bernstein.core.routing.router import load_model_policy_from_yaml

load_model_policy_from_yaml(Path("model_policy.yaml"), router)
```

The YAML structure:

```yaml
model_policy:
  allowed_providers: [list of provider names or null]
  denied_providers: [list of provider names or null]
  prefer: [single provider name or null]
```

## CLI

### `bernstein config validate`

Validates the entire configuration (model policy, providers, etc.):

```bash
$ bernstein config validate

# Output:
# ✓ Configuration is valid
#
# [Provider summary table]
```

Exit codes:
- `0` — Valid configuration
- `1` — Configuration issues found

## Design Principles

### 1. **Policy Before Routing**
The policy is evaluated **before** any routing algorithm (static or bandit) sees the providers. Denied providers are never offered as options.

### 2. **Clear Error Messages**
When a task cannot be routed (e.g., no provider available after policy), the error explicitly mentions the policy constraint:

```
RouterError: No available provider for model 'sonnet'
(All providers blocked by model_policy.denied_providers)
```

### 3. **Validation on Startup**
The router validates the policy when it initializes and logs warnings if there are issues (e.g., "Denied provider 'openai' is not registered").

### 4. **Preferred Provider Fallback**
If the preferred provider is unavailable (rate-limited, down), the router falls back to the next best allowed provider.

```python
policy = ModelPolicy(
    allowed_providers=["anthropic", "ollama"],
    prefer="anthropic"
)

# If anthropic is down → fallback to ollama
# If ollama is also down → no provider available → error
```

### 5. **Zero Ambiguity**
Policy rules are explicit and non-negotiable. No implicit fallbacks or guessing. If a policy says "only anthropic", only anthropic is used.

## Troubleshooting

### Problem: "No available provider" error

**Cause**: All providers are blocked by policy, or no providers exist for the required tier.

**Solution**:
1. Check `bernstein config validate` output
2. Verify policy doesn't deny all providers
3. Register more providers if needed
4. Check that at least one provider exists for each tier

### Problem: Task routed to unexpected provider

**Cause**: Policy doesn't have a preferred provider, or preferred provider is unavailable.

**Solution**:
1. Set `prefer` to your desired provider
2. Ensure preferred provider is in the allowed list
3. Check provider health (`bernstein config validate`)

### Problem: "Preferred provider not in allow list" error

**Cause**: Policy has `prefer: openai` but `allowed_providers: [anthropic]`.

**Solution**: Update preferred provider to be in the allowed list, or remove the prefer constraint.

## Performance

Model Policy is evaluated **once per routing decision** and is O(n) where n = number of denied/allowed providers. In practice, this is negligible (< 1ms).

The policy filter integrates into `get_available_providers()`, so there's no additional latency — the filtering happens during the normal provider selection flow.

## Audit Trail

Every routing decision respects the policy. You can audit which providers were considered:

```python
summary = router.get_provider_summary()

for name, info in summary.items():
    allowed = info["policy_allowed"]  # Boolean
    print(f"{name}: {'allowed' if allowed else 'blocked by policy'}")
```

## Peak-Hour Routing

Peak-hour routing (`src/bernstein/core/cost/peak_hour_router.py`) extends model policy with time-based constraints. It can automatically shift non-urgent tasks to cheaper providers or off-peak windows, complementing the static allow/deny policy.

## Quota Tracking

Provider quota tracking (`src/bernstein/core/cost/quota_tracker.py`) monitors per-provider spend and request counts. When a provider approaches its quota, the router can automatically shift traffic to other allowed providers within the model policy.

## Future: Dynamic Policy Updates

Model Policy is currently static (loaded at startup). Future versions may support:

- **Hot reload**: Update policy without restarting
- **Task-specific constraints**: "Role=security must use opus-only"
- **A/B testing**: "50% requests to anthropic, 50% to ollama"

## Related

- **Router** — `src/bernstein/core/routing/router.py` (re-exports from `router_core.py` and `router_policies.py`) — Core routing engine
- **TierAwareRouter** — Handles provider selection
- **Config Validation** — `bernstein config validate` command
- **DESIGN.md** — Overall architecture

## Summary

Model Policy gives enterprises surgical control over where code and data go. It's:

- **Transparent**: Policy is in YAML, auditable, version-controllable
- **Enforced**: Denied providers never touch your code
- **Validated**: `bernstein config validate` catches misconfigurations
- **Integrated**: Works seamlessly with the existing router

Use it to enforce compliance, data residency, cost control, or preferred vendors.
