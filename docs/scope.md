# Scope: what this project does not do

A project that never says no accumulates surface until nothing in it is
reliable. This page collects the boundaries that are already decided,
each pointing at the record that decided it, so a proposal can get an
answer in one link instead of a paragraph — and so anyone can tell,
before writing code, whether an idea has a home here.

These are current positions with stated reasons, not permanent law. A
proposal that addresses the reason is worth reading. A proposal that
only asserts the feature would be useful is not, because usefulness was
never the disagreement.

## No model in the coordination loop

Scheduling, task assignment, lifecycle management and retry logic are
deterministic Python. Zero tokens are spent on coordination.

That is what makes a run replayable: the same inputs produce the same
task graph, so a divergence between two runs is a bug you can chase
rather than a sampling artefact you can only shrug at. Anything that
would put a model between "a task finished" and "what runs next" is out
of scope, however good the model.

Record: [ADR-006](decisions/006-no-embedded-llm.md).

## No database for run state

All persistent orchestrator state is plain text under `.sdd/` — YAML,
Markdown, JSONL, JSON. No embedded database, no hidden in-memory state.

A feature that requires run state to move behind a schema is out of
scope. State you cannot open in an editor is state you cannot read
during the incident it matters for.

Record: [ADR-004](decisions/004-file-based-state.md).

## No long-lived agents

Agents spawn with a small batch of tasks, execute them, and exit. The
orchestrator spawns new ones as work appears.

Resident worker pools, persistent agent identities and agent-to-agent
negotiation are out of scope. The failure they reintroduce — a worker
that looks alive and produces nothing — is the one this design exists
to remove, and it is not a failure you notice quickly.

Record: [ADR-005](decisions/005-short-lived-agents.md).

## Extending does not mean forking

Extension happens through the pluggy hook surface. If you need a hook
that does not exist, the hook is the contribution; a fork that has to
be rebased forever is not an extension mechanism, it is a maintenance
sentence.

Record: [ADR-007](decisions/007-pluggy-plugin-system.md).

## Adapters wrap tools, they do not reimplement them

An adapter translates between this orchestrator and a coding CLI's
actual interface. It does not reimplement the tool's behaviour, work
around its bugs, or vendor its prompts. When a tool changes its flags,
the adapter changes. When a tool is wrong, that belongs in the tool's
own tracker.

A new adapter is welcome. An adapter for a tool that cannot be
exercised in CI is not, because an adapter nothing runs is a claim
rather than a feature.

See [adapter contracts](contributing/adapter-contracts.md).

## The dashboard talks to the orchestrator and nothing else

The web UI fetches records from the API and event stream it was pointed
at, and from nowhere else. Fonts are vendored. There are no third-party
asset hosts, no analytics, no telemetry.

Record: [web UI principles](design/web-ui-principles.md), P7.

## Not a model provider

This drives coding CLIs you have already installed and authenticated.
It does not proxy inference and does not resell tokens.

## Not an editor

There is a terminal UI and a web dashboard for watching and steering
runs. Neither is trying to become the place you write code.

---

If a boundary here is the only thing standing between you and something
you need, open an issue that argues with the reason rather than around
it. Several of these records exist because that argument was made and
won.
