# E46 — APT/YUM Repository

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Linux users on Debian/Ubuntu and RHEL/Fedora systems cannot install Bernstein through their native package managers, limiting adoption in server environments.

## Solution
- Create `.deb` and `.rpm` packages using `fpm` (Effing Package Management).
- Set up an APT repository and YUM repository hosted on GitHub Pages or Cloudflare R2.
- Add a CI pipeline step that builds packages and publishes to the repository on every release tag.
- Include GPG signing for package authenticity.
- Add installation instructions: `apt-get install bernstein` / `yum install bernstein`.

## Acceptance
- [ ] `.deb` package installs bernstein correctly on Ubuntu/Debian
- [ ] `.rpm` package installs bernstein correctly on RHEL/Fedora
- [ ] Packages are GPG-signed and verifiable
- [ ] CI pipeline auto-publishes packages on release
