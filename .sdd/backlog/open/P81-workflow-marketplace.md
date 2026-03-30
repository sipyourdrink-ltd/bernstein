# P81 — Workflow Marketplace

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Users recreate common workflow patterns from scratch because there is no shared repository of reusable bernstein.yaml templates to browse and fork.

## Solution
- Create a public workflow gallery backed by a GitHub repository (e.g., `bernstein-workflows`)
- Each workflow is a directory containing `bernstein.yaml`, `README.md`, and optional sample inputs
- Build a web UI page to browse, search (by name/tag/category), and preview workflows
- Add a "Fork" button that copies the workflow into the user's account or local project
- Implement `bernstein workflow browse` CLI command that lists and filters available templates
- Add `bernstein workflow install <template-name>` to download a template into the current project
- Tag workflows with categories: CI/CD, refactoring, testing, documentation, migration

## Acceptance
- [ ] Public GitHub-backed workflow repository with structured template directories
- [ ] Web UI page lists workflows with search and category filtering
- [ ] Workflow preview shows YAML content and README description
- [ ] Fork action copies workflow template into user's project
- [ ] `bernstein workflow browse` CLI lists available templates with filtering
- [ ] `bernstein workflow install <name>` downloads template to current directory
- [ ] Workflows tagged with categories for discoverability
