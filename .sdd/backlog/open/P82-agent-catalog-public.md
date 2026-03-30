# P82 — Public Agent Catalog

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Community members build agent adapters but have no centralized place to share them, making discovery difficult and limiting the agent ecosystem.

## Solution
- Build a public agent catalog for community-contributed adapters
- Each catalog entry includes: name, description, compatibility matrix (models, OS), star rating, install count
- Back the catalog with a JSON index file in a GitHub repository, updated via PR workflow
- Create a searchable web page with filters for compatibility, rating, and category
- Implement `bernstein agents browse` CLI command with text search and filters
- Add `bernstein agents install <adapter-name>` to install from the catalog
- Track install counts via download telemetry (opt-in)

## Acceptance
- [ ] Public agent catalog with structured JSON index in GitHub repository
- [ ] Each entry has name, description, compatibility, rating, and install count
- [ ] Web page lists agents with search, filter by compatibility and category
- [ ] `bernstein agents browse` CLI lists and searches available adapters
- [ ] `bernstein agents install <name>` installs adapter from catalog
- [ ] Install counts tracked and displayed
- [ ] Community can submit adapters via pull request to catalog repo
