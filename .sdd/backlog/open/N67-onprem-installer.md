# N67 — On-Prem Installer

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Enterprise on-prem deployments require a reliable, repeatable installation process, but Bernstein has no official installer — teams must manually install dependencies and configure the environment.

## Solution
- Create a shell installer script served at `curl -fsSL install.bernstein.dev | bash`
- Script detects OS (Ubuntu 22+, RHEL 8+, macOS) and architecture
- Installs Python if not present (via system package manager or pyenv)
- Installs Bernstein via `pipx` for isolated environment
- Runs `bernstein doctor` post-install to verify the installation
- Supports `--version` flag to pin a specific release

## Acceptance
- [ ] One-liner install command works: `curl -fsSL install.bernstein.dev | bash`
- [ ] Script detects and supports Ubuntu 22+, RHEL 8+, and macOS
- [ ] Python is installed automatically if missing
- [ ] Bernstein is installed via `pipx`
- [ ] `bernstein doctor` runs automatically after installation
- [ ] `--version` flag allows pinning a specific Bernstein release
