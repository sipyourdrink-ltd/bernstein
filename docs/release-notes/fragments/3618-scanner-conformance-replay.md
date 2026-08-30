## Scanner conformance harness replays for real

The golden-transcript harness for scanner adapters could not replay a step: it read a field its step type never defined, dropped the scan scope before calling the adapter, and looked determinism tiers up under a key the registry does not use. Steps now carry an explicit `target` and `scope` that reach `scan()` as given, pinned feed digests and transcripts are actually compared against the recorded run, and the whole replay path is covered by tests, so adapter authors can build against it. (#3618)
