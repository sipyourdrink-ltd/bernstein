# Ingest adapter contract

Bernstein ships a plugin hook (`provide_ingest_adapter`) and a declaration
dataclass (`IngestAdapterDeclaration`) that let external observability
integrations announce which event types they can receive. The declaration is
**static** — it is declared in code or YAML, never derived by introspecting a
running process — so a verifier can re-derive it from the receipt bytes alone,
without re-importing the plugin.

## IngestAdapterDeclaration

```python
from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

decl = IngestAdapterDeclaration(
    name="my-otlp-collector",
    version="1.0.0",
    declared_event_types=("gen_ai_activity", "untyped_activity"),
    summary="Forwards spans to an OTLP-compatible collector.",
)
```

| Attribute | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Short stable identifier for this ingest adapter. |
| `version` | `str` | Yes | Version string of the adapter. |
| `declared_event_types` | `tuple[str, ...]` | Yes | Event types this adapter declares it can receive. Each value must be a member of `INGEST_EVENT_TYPES`. |
| `summary` | `str` | No | One-line human-readable description. |

## Valid event types

Every declared event type must be a member of `INGEST_EVENT_TYPES` (defined in
[`src/bernstein/core/observability/ingest_contract.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/observability/ingest_contract.py)):

| Value | Meaning |
|---|---|
| `gen_ai_activity` | A span produced by a governed AI agent run (tool calls, LLM inferences, etc.). |
| `untyped_activity` | An activity event with no typed span structure. |

## Rejection rule

An adapter **cannot quietly widen what it claims to observe**. The
`declared_event_types` field is the contract: every event the adapter emits
must appear in that set. The ingest subsystem checks each incoming event
against the registered declaration; an event whose type is not in the
declaration is refused and the refusal is recorded in the audit chain, so a
verifier checking the receipt can confirm that the adapter's observed surface
matched its declaration.

## Receipt naming

Every ingested event records the adapter identity in its receipt. The receipt
carries `ingest.source_kind` (the adapter `name`) and `ingest.source_profile`
(the ingest profile name, e.g. `gen_ai_activity`) as attributes on the event,
so an operator or verifier can attribute every event back to the specific
adapter that produced it — without consulting the plugin code itself.

## Static-manifest decision

Declarations are made in Python code or YAML, not derived at runtime through
type introspection. Two reasons:

- **Offline auditability.** A verifier reading a receipt bundle has everything
  it needs to re-derive the declaration: the receipt bytes, the adapter name
  and version recorded in the event attributes, and the constant allowlist. No
  live import of the plugin is required.
- **Verification runs without the plugin.** Because the verifier does not
  re-execute `provide_ingest_adapter`, it cannot be misled by a plugin that
  behaves differently at verification time than it did at ingest time.

## Declaring via the plugin hook

Plugins implement `provide_ingest_adapter` and return one of:

- `None` — opt out.
- A single `IngestAdapterDeclaration` instance.
- A `(name, version, declared_event_types)` or
  `(name, version, declared_event_types, summary)` tuple.
- A list of any mix of the above.

```python
from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration
from bernstein.plugins import hookimpl


class MyIngestPlugin:
    @hookimpl
    def provide_ingest_adapter(self):
        return IngestAdapterDeclaration(
            name="my-otel-adapter",
            version="2.1.0",
            declared_event_types=("gen_ai_activity",),
            summary="OTLP exporter for gen_ai_activity spans.",
        )
```

The plugin manager collects declarations during discovery and stores them in
`bernstein.core.trackers.registry._ingest_declarations`. Duplicate names are
skipped with a warning; the first registration wins.

## Built-in adapter

Bernstein ships one built-in ingest adapter:

| Adapter | Module | Event types |
|---|---|---|
| `OTLPIngestAdapter` | `bernstein.core.observability.otlp_ingest` | `gen_ai_activity`, `untyped_activity` |

See [`OTLPIngestAdapter`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/observability/otlp_ingest.py)
for its wire format and the full ingest pipeline.
