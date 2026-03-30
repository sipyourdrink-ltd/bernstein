# D01 — `bernstein init` Interactive Wizard

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
New users have no guided way to create a bernstein.yaml config file. They must read docs and write YAML by hand, which creates friction and errors on first use.

## Solution
- Add a `bernstein init` command using Click prompts
- Auto-detect project type by checking for the presence of: `pyproject.toml` (Python), `package.json` (Node), `Cargo.toml` (Rust), `go.mod` (Go)
- Present detected project type and let user confirm or override
- Offer a list of workflow templates appropriate for the detected project type
- Prompt for essential config values (project name, default agent, task directory)
- Write a fully commented `bernstein.yaml` to the project root
- If `bernstein.yaml` already exists, warn and ask before overwriting

## Acceptance
- [ ] Running `bernstein init` in a Python project (with pyproject.toml) correctly detects "Python" as the project type
- [ ] Running `bernstein init` in a Node project (with package.json) correctly detects "Node" as the project type
- [ ] Running `bernstein init` in a Rust project (with Cargo.toml) correctly detects "Rust" as the project type
- [ ] Running `bernstein init` in a Go project (with go.mod) correctly detects "Go" as the project type
- [ ] Running `bernstein init` in a directory with no recognized config files prompts the user to select a project type manually
- [ ] The generated `bernstein.yaml` is valid and parseable
- [ ] Running `bernstein init` when `bernstein.yaml` already exists prompts before overwriting
