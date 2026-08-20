# MkDocs Material Coupling & End-of-Life Strategy

This document records the theme coupling inventory, portability analysis, and decision triggers for Material for MkDocs ahead of its scheduled end-of-life on **November 5, 2026**.

---

## 1. Upstream Context & Scope

Material for MkDocs will reach end-of-life on 5 November 2026. After this date, no security patches or maintenance will be published by upstream maintainers.

Because the documentation site is a published web surface rendering JavaScript, an unmaintained theme represents a long-term security maintenance risk. This inventory evaluates our current coupling to enable a rapid migration if triggered.

---

## 2. Portability Analysis: Content & Syntax

Across **489 markdown files** in `docs/`:

### Standard / Portable Extensions (~98.6% of corpus)
All configured Markdown extensions in `mkdocs.yml` use standard `Python-Markdown` or `pymdownx` packages:
- `admonition` (`!!! note`), `pymdownx.details`, `pymdownx.superfences`, `pymdownx.tabbed` (`=== "Tab"`), `pymdownx.highlight`, `pymdownx.snippets`, `pymdownx.tasklist`, `pymdownx.keys`, `pymdownx.mark`, `pymdownx.critic`, `attr_list`, `md_in_html`, `def_list`, `tables`, `footnotes`, `abbr`.

None of these belong exclusively to Material for MkDocs. They render natively under any standard MkDocs theme.

### Material-Bound Syntax (7 files / ~1.4% of corpus)
Only 7 files contain Material-bound markup — the section landing pages (`docs/index.md` plus each top-level section's `index.md`), which combine:
1. **Icon index syntax**: `:material-...` (all 7) and `:octicons-...` (6 of the 7).
2. **Grid cards**: `<div class="grid ...>` containers (all 7).

---

## 3. Template Overrides & Feature Flags

### Overrides (`docs/overrides/main.html`)
The single override file is 16 lines long:
```jinja
{% extends "base.html" %}

{% block extrahead %}
  <!-- 12 static <meta> tags: author, OpenGraph, Twitter card -->
{% endblock %}
```
This uses standard Jinja theme-extension patterns (`extends "base.html"` + `extrahead`) and contains no Material-specific macros.

### Feature Flags (19 Total in `mkdocs.yml`)
- **Load-bearing Information Architecture (4 flags)**:
  - `navigation.indexes`, `navigation.tabs`, `navigation.sections`: Shape section landing pages and multi-tier navigation.
  - `content.code.copy`: Provides copy buttons on code blocks.
- **Cosmetic / Non-critical (15 flags)**:
  - `navigation.instant`, `navigation.instant.progress`, `navigation.tracking`, `navigation.tabs.sticky`, `navigation.top`, `navigation.footer`, `navigation.prune`, `search.suggest`, `search.highlight`, `search.share`, `content.code.annotate`, `content.action.edit`, `content.tabs.link`, `toc.follow`, `announce.dismiss`.

---

## 4. Social Cards & Toolchain Dependencies

In `docs/requirements.in`:
```ini
mkdocs-material[imaging]>=9.7.7,<10
```
The `imaging` extra pulls `cairosvg` and `pillow` for Material's `social` card plugin. System packages (`libcairo2`, etc.) are installed in `.readthedocs.yaml`.

---

## 5. Re-evaluation Decision & Trigger Matrix

| Trigger | Condition | Required Action |
|---|---|---|
| **GHSA Security Advisory** | Any open CVE/GHSA against `mkdocs-material` | **Immediate Migration**: Migrate to a maintained successor theme (do not accept unpatched CVEs on published web surfaces). |
| **Build-Chain Block** | Incompatibility with Python or MkDocs versions required for docs build | **Forced Migration**: Upgrade theme to unblock build toolchain. |
| **Calendar Trigger** | Milestone `v3.19.0` (target 2026-10-08, prior to 5 Nov EOL) | **Decision Re-eval**: Review open advisories and confirm status. |
