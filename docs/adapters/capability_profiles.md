# Adapter capability profiles

A capability profile is a machine-readable declaration of what a CLI
agent supports and how it is invoked. The profile factory turns that
declaration into a working adapter, so tracking a new agent is a data
edit rather than a new hand-written module.

Source: [`src/bernstein/adapters/capability_profile.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/capability_profile.py).

## Why declarations

Adapters used to be one hand-written module per agent. That works until
the catalogue is large: every upstream rename, flag reshuffle or project
handover lands as a source edit, so the cost of tracking the ecosystem
scales with the size of the catalogue rather than with the size of the
change.

A profile moves the per-agent knowledge into data. The parts that are
genuinely shared - worker wrapping, environment isolation, log layout,
timeout watchdog, multimodal refusal, rate-limit metering - stay in one
implementation that every profiled agent gets for free.

Agents that do not fit the declarative shape keep their hand-written
module, and agents nobody has profiled at all are still served by the
`generic` CLI adapter. Neither path changes.

## The schema

| Field | Meaning |
|---|---|
| `name` | Registry key, e.g. `pydantic_ai` |
| `display_name` | Human-readable name returned by `CLIAdapter.name()` |
| `invocation` | The always-passed CLI surface (see below) |
| `implementation` | `FACTORY` (built from the profile) or `MODULE` (declaration over a hand-written adapter) |
| `mcp_client` / `mcp_server` | Consumes MCP servers / exposes itself as one |
| `native_subagents` | Can fan work out to its own subagents |
| `sandbox` | Strongest isolation tier: `none`, `process`, `container`, `vm` |
| `local_models` | Can drive a locally-hosted model |
| `vision` | Accepts image input |
| `computer_use` | Can drive a screen, mouse or keyboard |
| `max_parallel_workers` | Concurrent workers within one session; `1` is sequential |
| `agents_md` | Reads `AGENTS.md` project instructions |
| `resume` / `dangerous_mode` / `event_channel` | The three strategy axes shared with `STRATEGY_MATRIX` |
| `provides` | Provider-name aliases for routing |

`InvocationSpec` describes the **always-passed** surface - the tokens
every spawn emits:

```python
InvocationSpec(
    binary="clai",
    model_flag="-m",
    prompt_positional=True,
    extra_args=("--no-stream",),
    env_passthrough=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
)
```

Flags an adapter passes *conditionally* are deliberately out of scope.
They are not part of the pinned contract, and modelling them would make
the profile-to-contract cross-check report false drift.

## Content addressing

`profile.profile_hash` is a SHA-256 over the profile's canonical JSON
form (sorted keys, primitive values, `notes` excluded - prose about a
declaration is not part of the declaration). The hash is reachable from
a live adapter as `adapter.profile_hash`.

Recording that hash alongside a run makes a changed declaration show up
as a hash divergence with a named cause, instead of as unexplained
behaviour drift between two runs of the "same" adapter.

## Two implementation shapes

**`FACTORY`** - no hand-written module exists; the adapter is built from
the profile by `build_adapter_class_from_profile`. The factory returns a
*class*, not a shared instance, so every `get_adapter()` call yields a
fresh adapter with its own resource limits and rate-limit meter, exactly
as a hand-written adapter does.

**`MODULE`** - a hand-written module owns the spawn path and the profile
is the declaration of what that module supports. This is what makes
migration incremental and opt-in: an existing adapter gains a
machine-readable capability surface without any change to how it spawns.

## Gates a profile must clear

A profile is not self-certifying. Three checks apply, and none of them
was relaxed to admit profile-built adapters.

1. **Construction-time validation.** An underspecified profile cannot
   exist: a blank name or binary, an empty flag string, a prompt with no
   delivery mechanism, or `max_parallel_workers < 1` raises
   `ProfileValidationError` (a `ValueError` subclass) at construction.

2. **Contract cross-check.** `profile_contract_discrepancies` compares
   the declared invocation surface against the pinned contract YAML
   under `tests/contract/contracts/`. A profile may declare *less* than
   the contract requires, never more - it cannot claim a binary,
   subcommand or flag the contract does not carry.
   `assert_profile_backed_by_contract` raises on any discrepancy, so a
   declaration that outruns what was verified is refused rather than
   promoted.

3. **The existing conformance suite.** Profile-built adapters are
   ordinary registry entries. They resolve through `get_adapter`, are
   enumerated by `iter_adapter_specs`, and face the same
   `tests/unit/test_adapter_consistency.py` protocol suite, the same
   strategy declaration requirement, and the same contract gate as a
   hand-written adapter.

The profile test suite additionally asserts that every shipped profile
agrees with its `STRATEGY_MATRIX` row and that no profile claims vision
the multimodal capability table does not grant, so the declaration and
the authoritative matrices cannot drift apart.

## Capability-aware selection

`select_profile_for(requirements)` returns the first profile satisfying
a `TaskCapabilityRequirements`. A capability stronger than required
still satisfies it: a container-sandboxed adapter serves a task asking
for process isolation.

When nothing satisfies the task the call raises `CapabilityMismatchError`
rather than quietly falling back to a weaker adapter. The error carries a
`CapabilityRefusalReceipt` - a content-addressed record of the
requirements, every candidate considered with the profile hash it
presented, and the axes that went unmet:

```python
try:
    profile = select_profile_for(TaskCapabilityRequirements(vision=True))
except CapabilityMismatchError as exc:
    exc.receipt.unmet          # ("vision",)
    exc.receipt.receipt_hash   # deterministic sha256
```

The receipt hash is deterministic for a given
`(requirements, candidates, unmet)` triple, so two operators
reconstructing the same refusal derive the same identifier.

## Adding an agent as a profile

1. Add an `AdapterCapabilityProfile` to `_PROFILE_LIST` in
   `capability_profile.py`. Keep the declaration conservative: claim a
   capability only when the contract or upstream documentation backs it.
2. Add the contract YAML under `tests/contract/contracts/<name>.yaml`
   pinning the always-passed surface.
3. Add a `STRATEGY_MATRIX` row in `_contract.py` keyed by the same name.
4. Add a `use_cases.py` entry so `bernstein integrations list` has copy.
5. For a `FACTORY` profile, add the module to the
   `adapters-independent` contract only if you introduce a new module -
   profiles themselves add no module, which is the point.

Run `uv run pytest tests/unit/adapters/test_capability_profile.py` to
check the profile against every gate above.

## Shipped profiles

| Adapter | Shape | Always-passed surface |
|---|---|---|
| `pydantic_ai` | `FACTORY` | `clai -m <model> --no-stream <prompt>` |
| `droid` | `MODULE` | `droid <prompt>` |
| `kimi` | `MODULE` | `kimi --yolo -c <prompt>` |
| `opencode` | `MODULE` | `opencode run -m <model> --format json <prompt>` |
| `goose` | `MODULE` | `goose run --instruction <prompt>` |
