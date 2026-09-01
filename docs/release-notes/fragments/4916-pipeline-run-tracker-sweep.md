## Drive tracker pipeline from pipeline run command

`bernstein pipeline run` now resolves configured tracker adapters from the registry and drives `build_pipeline_from_yaml`, performing a non-blocking sweep across trackers. `--dry-run` prints the resolved pipeline without contacting trackers. Each sweep appends a `tracker_pipeline.sweep` record to the HMAC audit chain capturing the config digest, contacted trackers, claimed/released handoffs, and stage outcomes.

(#4916)
