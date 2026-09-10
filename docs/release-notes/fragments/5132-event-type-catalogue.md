## Webhook event types are declared in a catalogue, and an unknown one is ignored rather than guessed

The GitHub and GitLab webhook parsers each carried their own inline notion of
which source events they understood. An event type neither of them recognised
fell through whatever branch happened to be last, so the boundary between
"we map this" and "we do not" lived in control flow and was different on each
side. A packaged `event_catalogue.yaml` now declares the mapping per source,
validated on load and again when the builtin handlers register, so a malformed
or duplicated entry fails at startup instead of at the first webhook.

An event the catalogue does not name is now counted, logged once on first
sight, and ignored: `receive()` answers `ignored` and nothing is enqueued.
Previously an unrecognised type could reach the queue and be processed as
something it was not.

Every mapped event, and every replay-ledger journal row, now carries
`catalogue_content_hash` — the canonical-JSON SHA-256 of the catalogue that
produced it. A run's ingest decisions can therefore be tied to the exact
vocabulary in force at the time, so a later reader can tell an event that was
genuinely unknown from one that a since-edited catalogue would now map.
