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

That's it. You do **not** need a CLI coding agent installed yet - that comes in the first-run page.

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
    brew tap chernistry/tap
    brew install bernstein
    ```

    Bernstein is **not** in `homebrew-core`. The `tap` step is required.
    The tap lives at `github.com/chernistry/homebrew-tap`; `brew tap chernistry/tap`
    is the Homebrew short form.

=== "Fedora / RHEL (dnf)"

    ```bash
    sudo dnf copr enable alexchernysh/bernstein
    sudo dnf install bernstein
    ```

=== "Debian / Ubuntu (apt)"

    See the [Linux install guide](install-linux.md) for the COPR, Homebrew, pip/uv/pipx, and Docker channels.

=== "npm wrapper"

    ```bash
    npx bernstein-orchestrator
    ```

    Wraps the Python package; still requires Python 3.12+ on `$PATH`.

=== "Docker"

    ```bash
    docker run -v "$(pwd)":/workspace -p 8052:8052 \
      ghcr.io/sipyourdrink-ltd/bernstein -g "your goal"
    ```

=== "From source"

    ```bash
    git clone https://github.com/sipyourdrink-ltd/bernstein
    cd bernstein
    uv venv && uv pip install -e ".[dev]"
    ```

---

## One-liner installers

For fresh machines that don't have Python set up yet, the install scripts bootstrap
Python (via `pyenv` or system package), pipx, and Bernstein in one step.

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

Combine extras with brackets: `pip install 'bernstein[openai,docker,s3]'`.

---

## Verify it worked

```bash
bernstein --version
```

You should see a version number close to **2.16.1**. Then run the pre-flight check:

```bash
bernstein doctor
```

`doctor` checks Python version, port availability, your `$PATH`, and any installed CLI agents.
A clean run prints all green. If something fails, it tells you exactly which step to fix.

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
