Keep the model override unset in both retry paths when neither a prior task
model nor an operator pin is available. The adapter continues resolving its
configured default, rather than receiving an invented Claude tier. Existing
effort escalation, explicit pins and resolved provider-model retention remain
unchanged.
