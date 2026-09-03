## Codex adapter reports a sandbox refusal as a failed run, not a success

A codex run whose sandbox denied every shell command previously exited 0 and
reported success. Bubblewrap needs an unprivileged user namespace to start;
in a capability-dropped container without one, every model-issued command
failed while `codex exec` still emitted `turn.completed`. The adapter now
reads the event stream after the run and marks it `permission_denied` when
every `command_execution` carries bubblewrap's specific refusal, leaving a
partial failure or an ordinary non-zero exit (a failing test, a bad flag)
unmarked.

Also documented: codex >= 0.152 speaks only the Responses API. `OPENAI_BASE_URL`
is allow-listed for custom endpoints, but one serving only
`/v1/chat/completions` cannot drive codex at all -- now stated in the module
docstring and the adapter guide next to the env var (#5314).
