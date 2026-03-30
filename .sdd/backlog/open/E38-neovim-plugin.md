# E38 — Neovim Plugin

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Neovim users have no native integration for monitoring and controlling Bernstein runs from within their editor.

## Solution
- Create a Neovim plugin (Lua) at `editor-plugins/neovim/bernstein.nvim/`.
- Implement a floating window that shows real-time task status (task name, state, elapsed time).
- Define commands: `:BernsteinRun` (prompts for goal, starts a run), `:BernsteinStatus` (opens the floating status window), `:BernsteinDiff` (shows the latest diff in a split buffer).
- Use Neovim's built-in `vim.api.nvim_open_win()` for floating windows.
- Communicate with bernstein via the local API or by shelling out to the CLI.

## Acceptance
- [ ] `:BernsteinRun` prompts for a goal and starts a bernstein run
- [ ] `:BernsteinStatus` opens a floating window with current task status
- [ ] `:BernsteinDiff` shows the latest run diff in a buffer
- [ ] Plugin loads without errors in Neovim 0.9+
