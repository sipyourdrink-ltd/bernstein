# 532 — Bernstein as GitHub App: hosted orchestration for any repo

**Role:** architect
**Priority:** 3 (medium)
**Scope:** large

## Problem

Current Bernstein requires local installation. The lowest-friction way to get
adoption is a GitHub App: install it on your repo, and Bernstein manages your
agents automatically. This is how Copilot, CodeRabbit, Dependabot built massive
adoption.

## Design

### GitHub App functionality
- Install on repo -> Bernstein gets webhooks for:
  - New issues (auto-decompose into tasks)
  - PR comments (trigger review agents)
  - Push events (trigger CI-like agent workflows)
  - Label changes (evolve-candidate label triggers evolution)

### Hosted mode
- Bernstein server runs as a cloud service
- Each repo gets its own task namespace
- Agents spawned on-demand (cloud VMs or containers)
- Free tier: 10 tasks/month, 1 agent
- Pro tier: unlimited tasks, 5 concurrent agents

### Self-hosted mode
- Same GitHub App code, self-hosted
- Connect your own API keys
- Run on your own infra

### Integration with existing flow
- `bernstein.yaml` in repo configures the app
- `.sdd/` committed to repo for state persistence
- GitHub Actions can trigger Bernstein tasks

## Files to create
- New: `src/bernstein/github_app/` — webhook handlers, app auth
- `deploy/github-app/` — deployment config
- `.github/app.yml` — GitHub App manifest

## Completion signal
- GitHub App installable from GitHub Marketplace
- Issue created -> tasks auto-generated -> PR created
- At least 3 repos using the app
