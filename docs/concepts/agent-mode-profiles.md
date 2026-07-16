# Agent mode profiles

Different foundation models have distinct working styles. Claude likes
rapid feedback, GPT-5.2 runs deep solo, small fast models thrive with
narrow tool sets. **Mode profiles** sit between the model router and
the spawner: once a model is chosen, the matching profile dictates the
system-prompt preamble, the tool subset, the temperature, and the
turn budget.

## Why it exists

Bernstein routes tasks to models by cost / quality bandit signals.
Once the model is chosen, the same prompt and tool list go out
regardless of who's at the other end. That throws away an obvious
free win: tuning the interaction to the model's known personality.
This module is the small abstraction shared across all adapters
that lets one prompt-build pipeline produce three different shapes.

## How to use it

Profiles ship preinstalled under `templates/mode_profiles/`. The
default mapping is:

| Model family | Profile | Why |
|---|---|---|
| Claude (Code, Opus, Sonnet) | `smart` | rapid feedback, full tool surface |
| GPT-5.x, o-series | `deep` | longer turns, narrower tools, lower temp |
| Small / fast (Qwen, Haiku) | `fast` | one tool subset, short turn budget |

Override per task via tag:

```yaml
stages:
  - name: research
    steps:
      - role: backend
        goal: "Map every callsite of FooClient"
        tags: [mode:fast]
```

Inspect a model's resolved profile:

```python
from bernstein.core.routing.mode_profile import select_mode

profile = select_mode(model_id="claude-sonnet-4", task=task)
print(profile.name, profile.max_turns, profile.tool_subset)
```

Add a custom profile by dropping a YAML under `templates/mode_profiles/`:

```yaml
# templates/mode_profiles/researcher.yaml
name: researcher
system_prompt_preamble: |
  You are running in long-form research mode...
tool_subset: [read_file, grep, web_fetch]
temperature: 0.2
max_turns: 80
expected_runtime_minutes: 15
```

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.MODE_PROFILES_ENABLED` | `true` | Master switch. |
| `templates/mode_profiles/*.yaml` | three profiles shipped | Profile registry, loaded at startup. |
| Per-task tag `mode=<name>` | none | Force a profile for one task. |

Spawn metrics carry a `mode_profile` Prometheus label, so you can
graph `spawns_total{mode_profile="deep"}` to see distribution.

## Limitations

- Server-side only. There is no per-task UI customisation knob.
- One profile per spawn. No mid-session profile switching.
- The mode profile sits **after** model selection, not before. It
  does not influence which model is chosen.
- The tool-subset filter operates on tool names declared in the
  profile; tools added by hooks or plugins outside the profile list
  are filtered out.

## Related

- Source: `src/bernstein/core/routing/mode_profile.py`
- Profiles: `templates/mode_profiles/`
- Spawner: `src/bernstein/core/agents/spawner_prompt.py`
- [Model Routing](../architecture/model-routing.md)
- PR #1007
