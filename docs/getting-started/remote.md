# Remote quickstart (GitHub Codespaces)

The standard install paths (`pipx`, `uv`, `brew`, `docker`) all assume a
local terminal. If you do not have one to hand - reviewing on a phone,
sitting in a meeting on a borrowed laptop, or just curious without
committing to a local install - GitHub Codespaces provides a one-click
cloud sandbox that runs the project's devcontainer.

## Time: about 60 seconds

1. Click the **Open in Codespaces** badge in the project README.
2. Wait for the container to build. The first build takes roughly a
   minute; subsequent starts are fast. The `postCreate` step installs
   `bernstein` and runs `bernstein --help` so you can confirm the CLI
   is on the path.
3. In the integrated terminal:

   ```bash
   bernstein init --remote
   bernstein --help
   ```

   The `--remote` flag tells `bernstein init` to skip local-binary
   checks (such as the `brew` adapter probe) that are not expected to
   succeed inside a fresh container. The flag is also auto-enabled when
   `CODESPACES=true` is present in the environment.

4. Provide an adapter API key via Codespaces user secrets, then run a
   trivial goal:

   ```bash
   bernstein run -g "list files in the repo and summarise the layout"
   ```

## How the project recognises Codespaces

- `bernstein init --remote` short-circuits the local toolchain probe.
- `bernstein doctor --json` adds a `runtime` field with the value
  `codespace` when the process detects a Codespaces environment, and
  `local` otherwise. Use that field in your own scripts to decide which
  checks to enforce.
- The devcontainer sets `BERNSTEIN_REMOTE_QUICKSTART=1` so any other
  remote-container surface that follows the same convention is treated
  identically.

## Limitations

- The Codespaces session is yours, not a hosted Bernstein sandbox.
  You supply adapter API keys via your own Codespaces user secrets and
  are billed for compute by GitHub.
- The devcontainer pre-installs `bernstein` and `pipx`, but it does not
  install any third-party agent CLI. Pick whichever adapter you want
  (Claude Code, Codex, Gemini, etc.) and install it inside the
  container before running an agent task.
- A fresh container has no cached state. Plan-only or dry-run modes
  are a good way to confirm the setup before spending API budget.

## Going local later

When you are back at a real terminal, the standard local install paths
still apply:

```bash
pipx install bernstein
bernstein init
```

The two paths are interchangeable; the remote quickstart simply adds a
no-terminal entry point for users who would otherwise drop off at the
install step.
