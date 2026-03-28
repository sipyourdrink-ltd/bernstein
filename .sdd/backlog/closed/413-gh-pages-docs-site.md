# 413 -- GitHub Pages documentation site

**Role:** frontend
**Priority:** 2
**Scope:** large
**Complexity:** medium

## Problem
Bernstein needs a public-facing documentation site hosted on GitHub Pages. The README covers the basics, but a proper docs site converts browsers into users. Target audience: developers and small teams evaluating multi-agent orchestration tools -- technical but not necessarily deep in the internals.

## Design reference
Minimalist, premium feel inspired by modern SaaS landing pages (Vercel, Supabase, Linear):
- Light background (#fafafa), dark text (#1a1a1a)
- Accent color: violet tones for interactive elements
- Soft gradient orbs as background decoration (blur-100px, 50-60% opacity)
- Clean serif headings, system sans-serif body
- Subtle entrance animations (opacity + Y translate on scroll)
- Feature cards with large border-radius, semi-transparent white backgrounds
- Mobile-responsive, no framework dependencies (pure HTML/CSS/minimal JS)

## Pages
1. **Landing** (`index.html`):
   - Hero: tagline + `bernstein` CLI demo (animated terminal mockup)
   - "Who it's for" section with 3-4 persona cards
   - "How it works" flow diagram (ASCII or SVG)
   - Feature grid: parallel agents, self-evolution, agent catalogs, built-in verification
   - Comparison table vs CrewAI/AutoGen/LangGraph
   - Quick start code block
   - CTA: GitHub link

2. **Getting Started** (`getting-started.html`):
   - Installation
   - First run walkthrough
   - Configuration (bernstein.yaml)
   - Dashboard hotkeys

3. **Concepts** (`concepts.html`):
   - Architecture overview
   - Task lifecycle
   - Agent roles and routing
   - Self-evolution pipeline
   - Agent catalogs

4. **API Reference** (`api.html`):
   - Task server endpoints
   - CLI commands
   - Configuration schema

## Technical requirements
- Static HTML/CSS/JS -- no build step, no framework
- Hosted from `docs/` directory via GitHub Pages
- Responsive (mobile-first)
- Lightweight: < 100KB total (no heavy assets)
- Syntax highlighting for code blocks (highlight.js or Prism, inline)
- Smooth scroll + subtle entrance animations (CSS only, no Framer Motion)
- Navigation: minimal top bar with page links
- Dark mode toggle (CSS custom properties)

## Files
- docs/index.html
- docs/getting-started.html
- docs/concepts.html
- docs/api.html
- docs/style.css
- docs/script.js (minimal: dark mode toggle, scroll animations)

## Completion signals
- path_exists: docs/index.html
- path_exists: docs/style.css
- file_contains: docs/index.html :: Bernstein
- file_contains: docs/index.html :: viewport


---
**completed**: 2026-03-28 05:18:46
**task_id**: ad919628d72d
**result**: Completed: 413 -- GitHub Pages documentation site
