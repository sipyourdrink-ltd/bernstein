# 636 — Agent Profile Marketplace

**Role:** frontend
**Priority:** 4 (low)
**Scope:** medium
**Depends on:** #608

## Problem

Agent configurations (roles, system prompts, model preferences) are project-local with no sharing mechanism. Roo Code's Mode Gallery proved that community-shared agent configurations drive engagement and adoption. Users repeatedly reinvent similar agent setups.

## Design

Create an agent profile marketplace where users can share and discover agent definitions. An agent profile includes: role name, system prompt, recommended model, typical tasks, and success metrics. Profiles are stored as structured markdown files following a standard schema. The marketplace is initially a curated directory in the repo (`templates/marketplace/`) with a simple CLI browser (`bernstein marketplace list`, `bernstein marketplace install <profile>`). Installing a profile copies it to the project's `templates/roles/` directory. Community contributions via PR. Rate profiles by usage count and success rate. Include a quality check: profiles must include at least one example task and expected outcome. Future: host on a web registry for easier discovery.

## Files to modify

- `templates/marketplace/` (new directory with curated profiles)
- `src/bernstein/cli/marketplace.py` (new)
- `src/bernstein/core/profile.py` (new)
- `docs/marketplace.md` (new)

## Completion signal

- `bernstein marketplace list` shows available community profiles
- `bernstein marketplace install <name>` installs a profile locally
- At least 10 curated profiles available at launch
