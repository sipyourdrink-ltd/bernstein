# E40 — Emacs Package

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Emacs users have no package for editing bernstein.yaml files with syntax support or for controlling Bernstein runs interactively.

## Solution
- Create an Emacs package at `editor-plugins/emacs/bernstein-mode.el`.
- Implement `bernstein-mode` as a major mode for `bernstein.yaml` files with syntax highlighting (keywords, keys, values).
- Add interactive commands: `M-x bernstein-run` (prompts for goal, runs in a `compilation-mode` buffer), `M-x bernstein-status` (shows current run status in a dedicated buffer).
- Use `compilation-mode` for output so users get clickable error links.
- Register the mode for `bernstein.yaml` and `bernstein.yml` files via `auto-mode-alist`.

## Acceptance
- [ ] `bernstein-mode` activates automatically for bernstein.yaml files
- [ ] Syntax highlighting works for bernstein.yaml keywords
- [ ] `M-x bernstein-run` executes a run and displays output in compilation-mode
- [ ] `M-x bernstein-status` shows the current run status
