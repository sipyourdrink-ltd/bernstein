Keep the model override unset in both retry paths when neither a prior task
model nor an operator pin is available. The adapter continues resolving its
configured default, rather than receiving an invented Claude tier. Original
effort overrides are preserved alongside an unresolved model so an effort-only
override cannot bypass the original template resolution. Known-model effort
escalation, explicit pins and resolved provider-model retention remain unchanged.
