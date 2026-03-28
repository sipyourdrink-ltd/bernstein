# 504 — Align dashboard SVG mockup with real TUI

**Role:** frontend
**Priority:** 2
**Scope:** medium
**Complexity:** medium

## Problem
README dashboard SVG has 13 discrepancies with real TUI. Missing: Activity section, Sparkline, Chat input, Footer. Shows fake pid/filepath that don't exist in real code. This misleads users about what they'll actually see.

## Two options (choose one):

### Option A: Update SVG to match real TUI (easier)
Regenerate dashboard.svg to show the actual layout:
- Two columns on top (AGENTS | TASKS) — matches real `#top-panels`
- Activity bar below — matches real `#activity-bar`
- Bottom bar: stats + sparkline + chat input — matches real `#bottom-bar`
- Footer with keybindings (q r s l c)
- Agent widgets show: status dot + role + model + runtime + task arrows + log tail (not fake pid/filepath)
- Task table: 3 columns (icon | ROLE | TASK) with zebra stripes

### Option B: Upgrade real TUI to match SVG (harder, better result)
The SVG version actually looks better in some ways. Consider:
- Add agent PID and primary file display to AgentWidget
- Clean up agent widget layout to match SVG's cleaner presentation
- Keep the real layout (Activity, Sparkline, Chat) since those are features

Recommendation: Option A first (quick fix), then gradually improve TUI toward Option B style.

## Files
- docs/assets/dashboard.svg — regenerate to match reality
- src/bernstein/cli/dashboard.py — optional TUI improvements

## Completion signals
- path_exists: docs/assets/dashboard.svg
