## Reverse lookup disambiguation for multi-key adapter classes

Adapter instances obtained via `get_adapter(key)` now carry their selected `registry_name`, ensuring `registry_name_for` reliably resolves the selected key rather than guessing based on dictionary insertion order when a class is registered under multiple keys (such as `GeminiAdapter` under `antigravity` and `gemini`) (#5497).
