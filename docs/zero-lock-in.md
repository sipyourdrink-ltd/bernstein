# Zero Lock-in: Model-Agnostic Agent Orchestration

Every major AI lab ships its own agent framework:

- **OpenAI** has the Agents SDK (works with OpenAI models)
- **Google** has ADK (works with Gemini models)
- **Anthropic** has the Claude Agent SDK (works with Claude models)

They all solve the same problem — orchestrating AI agents — but each one locks you into a single provider's models. If you build on one and the pricing changes, or a competitor releases something better, you rewrite your orchestration layer.

Bernstein takes a different approach. The orchestrator is plain deterministic Python code. The agents are CLI processes. Any CLI that can accept a prompt and write to stdout works.

## The adapter interface

Every CLI agent in Bernstein implements one abstract class with four methods:

```python
class CLIAdapter(ABC):
    @abstractmethod
    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        """Launch an agent process with the given prompt."""
        ...

    def is_alive(self, pid: int) -> bool:
        """Check if the agent process is still running."""
        ...

    def kill(self, pid: int) -> None:
        """Terminate the agent process."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this CLI adapter."""
        ...
```

`is_alive` and `kill` have sensible defaults (POSIX signals). You only need to implement `spawn` and `name` to get a working adapter.

Source: [`src/bernstein/adapters/base.py`](../src/bernstein/adapters/base.py)

## Built-in adapters

Bernstein ships with adapters for five agent CLIs:

| Adapter | CLI | Provider |
|---------|-----|----------|
| `ClaudeCodeAdapter` | `claude` | Anthropic |
| `CodexAdapter` | `codex` | OpenAI |
| `GeminiAdapter` | `gemini` | Google |
| `QwenAdapter` | `qwen` | Alibaba / OpenAI-compatible |
| `GenericAdapter` | anything | any CLI that takes a prompt flag |

Each adapter translates the same `spawn()` call into provider-specific CLI flags. For example, Claude Code needs `--model`, `--effort`, `--dangerously-skip-permissions`, and `--output-format stream-json`. Codex needs `--model`, `--approval-mode full-auto`, and `--quiet`. Gemini needs `--model`, `--sandbox none`, and `--prompt`. The adapters handle these differences so the orchestrator never sees them.

## Adding a new adapter

Suppose a new agent CLI called `acme-agent` ships tomorrow. Here is a complete adapter:

```python
class AcmeAdapter(CLIAdapter):
    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["acme-agent", "--model", model_config.model, "--prompt", prompt]

        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return SpawnResult(pid=proc.pid, log_path=log_path)

    def name(self) -> str:
        return "Acme Agent"
```

Register it:

```python
from bernstein.adapters.registry import register_adapter
register_adapter("acme", AcmeAdapter)
```

Done. Every feature of Bernstein — task routing, worktree isolation, conflict-aware merging, the janitor, tracing — works with this adapter unchanged.

## The GenericAdapter: zero code required

If you do not want to write a Python class at all, use `GenericAdapter`. It is a configurable adapter that works with any CLI:

```python
from bernstein.adapters.generic import GenericAdapter

adapter = GenericAdapter(
    cli_command="aider",
    prompt_flag="--message",
    model_flag="--model",
    extra_args=["--yes-always", "--no-git"],
    display_name="Aider",
)
```

This launches `aider --model <model> --yes-always --no-git --message <prompt>`. No subclass needed.

## Multi-provider routing

Bernstein does not just support multiple providers — it routes different tasks to different providers within the same run. The `TierAwareRouter` scores providers on health, cost, latency, and free-tier availability, then picks the best one per task.

The routing logic in the spawner:

```python
# Route based on highest-complexity task in batch
base_config = _select_batch_config(tasks, templates_dir=templates_dir)

if self._router is not None and self._router.state.providers:
    decision = self._router.select_provider_for_task(
        tasks[0], base_config=base_config
    )
    model_config = decision.model_config
    provider_name = decision.provider
```

What this means in practice: a `manager` task that needs deep reasoning gets routed to Opus. A `qa` task that runs linting gets routed to Haiku or a free-tier Gemini model. A `backend` task gets Sonnet. All in the same orchestration run, no configuration changes between tasks.

Task-level overrides also work. The manager agent can pin a specific task to a model:

```python
@dataclass
class Task:
    ...
    model: str | None = None   # "opus", "sonnet", "haiku"
    effort: str | None = None  # "max", "high", "medium", "low"
```

When `model` or `effort` is set on a task, the router respects it. When they are `None`, heuristic routing kicks in based on complexity, scope, and role.

## Provider health and failover

The router tracks provider health in real time:

```python
@dataclass
class ProviderHealth:
    status: ProviderHealthStatus  # healthy, degraded, unhealthy, rate_limited, offline
    consecutive_failures: int
    consecutive_successes: int
    avg_latency_ms: float
    error_rate: float
    success_rate: float
```

If a provider starts failing, the router automatically falls back:

1. Try the preferred tier (free tier by default)
2. Fall back to standard tier
3. Fall back to premium tier
4. Last resort: use any available provider, even degraded ones

This means if your primary provider goes down mid-run, Bernstein continues with the next available option. No manual intervention.

## Why this matters

**Switch providers without rewriting orchestration.** Your task definitions, role templates, completion signals, janitor checks, evolution loop — none of it changes when you swap from Claude to Gemini to Codex.

**Use the right model for each task.** Not every task needs the most expensive model. Bernstein routes simple tasks cheaply and complex tasks to capable models. This is not possible when you are locked into a single provider's framework.

**Survive provider outages.** Rate limited on Anthropic? The router shifts to OpenAI or Google. Free tier exhausted on one provider? It uses paid tier on another. The orchestration keeps running.

**Evaluate new models by deploying them.** Register a new adapter, assign it to a few tasks, compare traces. No migration project, no rewrite.

The adapters live in [`src/bernstein/adapters/`](../src/bernstein/adapters/). The registry is in [`registry.py`](../src/bernstein/adapters/registry.py). The router is in [`src/bernstein/core/router.py`](../src/bernstein/core/router.py).
