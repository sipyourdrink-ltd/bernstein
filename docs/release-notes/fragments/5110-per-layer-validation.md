Per-layer schema validation before merge (#5110 Slice 3).

Overlay and inline configuration layers are now validated against their Pydantic
schemas before they are merged into the effective configuration. Invalid values
raise `LayerValidationError` naming the offending layer and source path, so
operators see which layer introduced the bad value.
