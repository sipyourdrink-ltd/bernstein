# Bernstein — Project Context

Multi-agent orchestration for CLI coding agents (Claude Code, Codex, Gemini, etc.).
Users describe a goal, Bernstein hires a team and ships the code.

## Tech stack
- Python 3.12+, FastAPI, Click, Rich
- Strict typing (Pyright strict), Ruff linting
- File-based state (.sdd/ directory)

## Current state
Phase 1 — core implementation needed.

## Key files
- src/bernstein/core/models.py — data models (Task, Cell, AgentSession)
- src/bernstein/core/router.py — model/effort selection
- src/bernstein/adapters/base.py — CLI adapter interface
- src/bernstein/adapters/claude.py — Claude Code adapter
- templates/roles/ — role system prompts
- docs/DESIGN.md — full architecture
