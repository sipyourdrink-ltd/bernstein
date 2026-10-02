## `cli: antigravity` spawns the Antigravity CLI (`agy`)

The `antigravity` registry key used to resolve to the Gemini adapter, which
looked for a binary named `antigravity` and passed Gemini flags (`-m`,
`--yolo`) that the Antigravity CLI does not accept. The key now resolves to
the `agy` adapter, and the Gemini adapter resolves only the `gemini` binary
(or `BERNSTEIN_GEMINI_BINARY`). `cli: gemini` is unchanged. See
`docs/adapters/antigravity.md`.
