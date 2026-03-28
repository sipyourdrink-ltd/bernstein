# 334g — Worktree Environment Setup Hooks
**Role:** backend  **Priority:** 1 (critical)  **Scope:** small

## Problem
3 sources: worktrees missing node_modules, .venv, .env — agents fail immediately.

## Design
On worktree creation: symlink shared dirs (node_modules, .venv), copy .env files, handle port conflicts, run project setup command. Configurable in bernstein.yaml.
