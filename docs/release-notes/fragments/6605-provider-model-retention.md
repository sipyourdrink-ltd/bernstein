## Retain supplied provider models across task retries

Both retry task creators preserve an already supplied provider-prefixed model
instead of manufacturing a bare Claude tier. Explicit role/run model pins keep
their priority, and existing effort, retry limits and backoff remain unchanged.
This is a partial fix for #6605: missing effective model context still needs
propagation when neither the task nor an explicit pin identifies the model.
