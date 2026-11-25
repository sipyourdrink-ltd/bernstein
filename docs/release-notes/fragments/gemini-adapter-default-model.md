## Set a default model on the Gemini CLI adapter

`GeminiAdapter` now declares `default_model = "gemini-3-flash"` so zero-config runs and `bernstein demo --real --adapter gemini` coerce unpinned tier defaults to a supported Gemini model instead of failing at spawn time with `ModelNotConfiguredError`.
