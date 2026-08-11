---
search:
  boost: 2
---

# Install Bernstein

**What this page does**: Gets Bernstein installed on your machine, then verifies it runs.
**Time**: ~2 minutes (the install) + ~30 seconds (the check).

When you're done, `bernstein --version` will print a version number and you'll be ready
to follow the [first-run walkthrough](first-run.md).

---

## Requirements

- **Python 3.12 or later**. `python3 --version` to check.
- **Git** (any recent version). Bernstein uses git worktrees to isolate agents.
- **macOS, Linux, or Windows**.

That's it for installing. Before your **first run** you will also need at least one
CLI coding agent (Claude Code, Codex CLI, Gemini CLI, ...) and its API key - the
[first-run walkthrough](first-run.md) covers that. You do **not** need them to
complete this page.

### Supported platforms

| Platform | Status | Process management |
|----------|--------|--------------------|
| Linux | Supported | POSIX process groups; graceful stop with force-kill escalation |
| macOS | Supported | POSIX process groups; graceful stop with force-kill escalation |
| Windows | Supported | Native process-tree termination (no POSIX signals required); `.cmd`/`.bat` adapter shims resolved automatically |

Windows notes:

- Worktree directory sharing via symlinks (`node_modules`, `.venv`) requires
  Developer Mode or Administrator privileges; without it, agents fall back to
  per-worktree installs.
- Deep worktree paths are handled with extended-length path support; no
  registry changes are needed.
- Forced agent stops are recorded in the audit chain with the same receipt
  format on every platform, so run histories verify identically across
  mixed-OS teams.

---

## Recommended: `uv tool install`

`uv` installs Bernstein into an isolated tool environment. Single command, no venv to manage.

```bash
uv tool install bernstein
```

If you don't have `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
irm https://astral.sh/uv/install.ps1 | iex        # Windows PowerShell
```

---

## Other install methods

=== "pipx"

    ```bash
    pipx install bernstein
    ```

=== "pip"

    ```bash
    pip install bernstein
    ```

=== "Homebrew (macOS / Linux)"

    ```bash
    brew install chernistry/tap/bernstein
    ```

    The formula builds its own virtualenv and resolves the runtime closure from
    wheels, so the install takes a couple of minutes and compiles nothing.

=== "Fedora / RHEL (dnf)"

    **Not yet available.** The COPR repository exists but does not serve a
    `bernstein` package yet. Tracked in
    [#3558](https://github.com/sipyourdrink-ltd/bernstein/issues/3558).
    Use `uv tool install bernstein` or `pipx install bernstein` in the meantime.

=== "Debian / Ubuntu"

    There is no APT repository. On Debian/Ubuntu, install with pip/uv/pipx or
    Docker - see the [Linux install guide](install-linux.md).

=== "Docker"

    ```bash
    docker run -v "$(pwd)":/workspace -v "$(pwd)/.sdd":/workspace/.sdd \
      -p 8052:8052 ghcr.io/sipyourdrink-ltd/bernstein -g "your goal"
    ```

    The explicit `.sdd` mount keeps run state in your project directory. On
    images up to 3.14.159 it is **required**: without it the container creates
    a root-owned volume at `/workspace/.sdd` and the run fails with
    `Permission denied`. Run from a non-default git branch
    ([first run](first-run.md) explains why).

=== "From source"

    ```bash
    git clone https://github.com/sipyourdrink-ltd/bernstein
    cd bernstein
    uv venv && uv pip install -e .
    source .venv/bin/activate
    bernstein --version
    ```

    For a development install (tests, linters, the `.[dev]` extras - which
    need a C toolchain) see
    [CONTRIBUTING.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/CONTRIBUTING.md).

=== "Dev container"

    The repo ships a `.devcontainer/` (Python 3.12, pipx, port 8052 forwarded)
    for GitHub Codespaces / VS Code Dev Containers. It is aimed at working
    **on** Bernstein itself; to use Bernstein in your own project, pick one of
    the other methods.

---

## Run without installing

- `uvx bernstein` - downloads the latest release into a temporary environment
  and runs it. Needs [`uv`](https://docs.astral.sh/uv/).
- `npx bernstein-orchestrator` - Node wrapper that delegates to an existing
  Python setup. It does **not** install Bernstein: it requires Python 3.12+
  **and** `pipx` or `uv` on `$PATH`, then runs Bernstein through them.

---

## One-liner installers

On a machine that already has Python 3.12+, the install scripts set up pipx and
Bernstein in one step. They **check** for Python 3.12+ and stop with an error if
it is missing - they do not install Python itself.

```bash
curl -fsSL https://bernstein.run/install.sh | sh           # macOS / Linux
irm https://bernstein.run/install.ps1 | iex                # Windows PowerShell
```

Script source: [install.sh](https://github.com/sipyourdrink-ltd/bernstein/blob/main/scripts/install.sh)
· [install.ps1](https://github.com/sipyourdrink-ltd/bernstein/blob/main/scripts/install.ps1).

---

## Optional extras

The base install stays small. Pull in provider SDKs only when you need them:

| Extra | Enables |
|-------|---------|
| `bernstein[openai]` | OpenAI Agents SDK v2 adapter |
| `bernstein[docker]` | Docker sandbox backend |
| `bernstein[e2b]`    | [E2B](https://e2b.dev) microVM sandbox |
| `bernstein[modal]`  | [Modal](https://modal.com) serverless containers |
| `bernstein[s3]`     | S3 artifact sink |
| `bernstein[gcs]`    | Google Cloud Storage artifact sink |
| `bernstein[azure]`  | Azure Blob artifact sink |
| `bernstein[r2]`     | Cloudflare R2 artifact sink |
| `bernstein[grpc]`   | gRPC bridge |
| `bernstein[k8s]`    | Kubernetes integrations |

Combine extras with brackets: `pip install 'bernstein[openai,docker,s3]'`.

## Editor extensions

- [VS Marketplace](https://marketplace.visualstudio.com/items?itemName=alex-chernysh.bernstein)
- [Open VSX](https://open-vsx.org/extension/alex-chernysh/bernstein)

---

## Verify it worked

```bash
bernstein --version
```

You should see the current release version. Then run the pre-flight check:

```bash
bernstein doctor
```

`doctor` checks your setup end to end: installed agent CLIs (adapters), API keys,
port availability, the `.sdd` workspace, and supporting tools.

Straight after an install - before you have configured an agent CLI or an API
key - **expect red rows** for adapters, auth, the workspace, and "Ready to run",
and a non-zero exit code. Those clear as you work through the
[first run](first-run.md). At this stage you only need `bernstein --version`
working and the Python and port checks green. Each failing row names the step
to fix.

> **`command not found: bernstein`**
> Your tool bin directory is not on `$PATH`. Add it:
>
> - `uv` / `pipx`: `export PATH="$HOME/.local/bin:$PATH"` (and add to `~/.zshrc` / `~/.bashrc`)
> - Windows: re-open PowerShell after install - pipx adds the path on first run.
> - macOS Homebrew: run `brew doctor` and follow the PATH advice.

---

## Next

Now that `bernstein --version` works, head to **[First run](first-run.md)** to take it from
"installed" to "your first orchestrated task complete" in about 5 minutes.

For platform-specific notes, see also:

- [Linux install (COPR / Homebrew / pip / Docker)](install-linux.md)
