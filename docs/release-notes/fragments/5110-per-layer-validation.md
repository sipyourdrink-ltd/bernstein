Per-layer schema validation before merge (#5110 Slice 3).

Configuration layers across both run-overlay merging and the resolution cascade
(session environment variables, project config, context, and global settings)
are now validated against dedicated section schemas and top-level Field constraints
before they are merged. Invalid values raise `LayerValidationError` naming the
offending layer and source path, so operators see which layer introduced the bad value.

