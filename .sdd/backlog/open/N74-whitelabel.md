# N74 — White-Label Branding

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Enterprise OEM partners want to rebrand Bernstein as their own product, but the name, logo, and colors are hardcoded throughout the CLI and web dashboard.

## Solution
- Add `branding:` section to bernstein.yaml with fields: `name`, `logo_url`, `primary_color`
- Web dashboard reads branding config and applies custom name, logo, and theme color
- CLI output replaces "Bernstein" with the configured custom name in banners and help text
- Provide sensible defaults that match the standard Bernstein branding
- Support branding override via environment variables for CI contexts

## Acceptance
- [ ] `branding:` section in bernstein.yaml accepts name, logo URL, and primary color
- [ ] Web dashboard displays the custom name and logo
- [ ] Web dashboard applies the custom primary color to the theme
- [ ] CLI output uses the custom name in banners and help text
- [ ] Default branding matches standard Bernstein if no custom values are set
- [ ] Environment variables can override branding config
