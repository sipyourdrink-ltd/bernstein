## OpenCode adapter now qualifies or refuses bare model ids before spawn

`opencode run -m <id>` resolves the id as `provider/model`, so a bare id
without a provider prefix (e.g. `my-model` from `role_model_policy.<role>.model`
or run-level `--model`) was accepted at the CLI surface but failed inside the
server as an opaque `UnknownError` before any model call. The adapter now
reads the operator opencode config (`~/.config/opencode/opencode.jsonc` or
`OPENCODE_CONFIG` / `OPENCODE_CONFIG_DIR` if set) and either qualifies the id
to the uniquely matching `provider/model` or refuses the spawn with a
legible error naming the expected config path. Qualified ids are passed
through unchanged. (#5350)
