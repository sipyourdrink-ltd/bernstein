## New plugin hook for ingest adapter declarations

Plugins can now announce which observability event types they can receive by
implementing `provide_ingest_adapter`, returning an `IngestAdapterDeclaration`
dataclass that names the adapter, version, and declared event types
(`gen_ai_activity` and/or `untyped_activity`). An adapter that emits a type
outside its declaration is refused at ingest time. Every ingested event
records the source adapter name and version in its receipt, so the declaration
is verifiable offline without re-importing the plugin. (#4963)
