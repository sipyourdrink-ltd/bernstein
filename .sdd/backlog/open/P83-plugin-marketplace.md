# P83 — Plugin Marketplace

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Plugins exist on PyPI but are hard to discover without a dedicated browsing experience that shows relevance, quality signals, and install instructions.

## Solution
- Build a plugin marketplace web page with search and category filters (verifiers, formatters, reporters, integrations)
- Pull metadata from PyPI API for packages matching `bernstein-plugin-*` naming convention
- Augment with a custom JSON index providing category tags, description overrides, and curated collections
- Display GitHub stars as a quality/popularity signal (fetched via GitHub API)
- Show one-click install instructions (`pip install bernstein-plugin-foo` / `bernstein plugin install foo`)
- Cache PyPI and GitHub metadata with 1-hour TTL to avoid rate limits
- Include a "Submit Your Plugin" link pointing to contribution guidelines

## Acceptance
- [ ] Web page lists plugins with search and category filtering
- [ ] Plugin metadata sourced from PyPI API for `bernstein-plugin-*` packages
- [ ] Custom JSON index provides category tags and curated collections
- [ ] GitHub stars displayed as popularity signal
- [ ] Each plugin shows install command for pip and bernstein CLI
- [ ] Metadata cached with 1-hour TTL
- [ ] "Submit Your Plugin" link and contribution guidelines available
