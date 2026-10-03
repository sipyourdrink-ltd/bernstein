## Config layers accept every selectable adapter and effort level

Per-layer validation introduced for session, project, global, overlay and
inline-override config checked `cli` against a fixed list (claude, codex,
gemini, qwen, auto) and `effort` against `max | medium | low`. A value the
seed parser and the router already accept -- `BERNSTEIN_CLI=aider`,
`cli: cursor` in a run overlay, or `effort: high` -- therefore failed to load
with a layer validation error. `cli` is now validated against the live adapter
registry plus `auto` (the same source the seed parser and `--cli` use), and
`effort` accepts `max`, `high`, `medium`, `normal` and `low`. Unknown values
are still rejected.
