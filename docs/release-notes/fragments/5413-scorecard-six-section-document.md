## Replay scorecard: six-section document with journal event-index citations

`bernstein.core.replay.scorecard` defines the `Scorecard` document type — a six-section run summary
(trajectory, verification, recovery, state_consistency, safety, replayability) that collapses every
replay substrate into one deterministic, citation-bearing artefact. Each section carries a
`citations` array of `Citation` rows (one per non-None field), where each citation names the
journal `seq` and `step_hash` of the source row, so a verifier that disagrees on the source of a
field can refuse the scorecard the same way it refuses a run receipt whose embedded bytes do
not re-derive the binding block.

The wire shape is pinned by a JSON Schema at
`https://bernstein.run/schemas/scorecard/v1` (`src/bernstein/core/replay/scorecard_schema.json`),
shipped under that stable `$id` and validated by
`tests/unit/core/replay/test_scorecard_validation.py`. `Scorecard.canonical_bytes` returns the
sorted-keys, compact-separator, UTF-8 bytes; wall-clock fields are kept on the document for
display but are stripped from the canonical bytes so two builds of the same recorded run produce
byte-identical artefacts. `Scorecard.from_dict` is the inverse of `to_dict` and `to_dict` ->
`from_dict` -> `canonical_bytes` is a fixed point (`tests/unit/core/replay/test_scorecard_serialization.py`).
The scorecard is not itself signed in this module; the signed envelope that binds it to the
journal and spine is the run receipt (`bernstein.core.replay.run_receipt`) (#5413).
