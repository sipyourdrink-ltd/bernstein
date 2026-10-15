# OpenClaw

The `openclaw` adapter runs [OpenClaw](https://github.com/openclaw/openclaw)
as a coding agent: one embedded agent turn per task, with no Gateway daemon.

```
openclaw agent exec --message-file <prompt> --cwd <worktree> --json --timeout <s>
```

`--cwd` makes the task's worktree both OpenClaw's workspace and its tools'
working directory, so the unit of work is the diff it leaves there, as for
every coding adapter. `--json` prints one result envelope (final text, usage,
cost, tool summary) to the session log. Exit code 0 is success, 1 a model or
result error, 2 OpenClaw's own timeout, which the adapter sets to the task's
watchdog timeout.

Install: `npm install -g openclaw` (Node 24.16+ or 26.1+).

## Models through the gateway

| Variable | Meaning |
|---|---|
| `BERNSTEIN_OPENCLAW_OPENAI_BASE_URL` | OpenAI-compatible endpoint |
| `BERNSTEIN_OPENCLAW_OPENAI_API_KEY` | this agent's key |
| `BERNSTEIN_OPENCLAW_MODEL` | model on that endpoint (default: the task's model) |

With the endpoint set, the adapter writes
`.sdd/runtime/openclaw-<session>.json` declaring it as an OpenAI-compatible
provider, and runs with `--config` on that file and `--auth-env-only`: the
operator's `~/.openclaw` configuration and stored credentials take no part.
The file names the key variable (`${BERNSTEIN_OPENCLAW_OPENAI_API_KEY}`),
which OpenClaw fills in from the environment, so the key is never written to
disk or put on the command line. Setting the endpoint or key without the
other, or with no model, fails the spawn and names the missing variables.

Without these variables OpenClaw's own configuration picks the provider, and
the task's model (unless `auto`) is passed with `--model`.

## Limits

- No resume: `agent exec` is stateless per run.
- One result envelope, no event stream: progress is not visible until the
  turn ends.
