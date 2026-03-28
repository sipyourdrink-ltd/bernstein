# 700 — Killer Demo GIF + Video

**Role:** docs
**Priority:** 0 (urgent)
**Scope:** small
**Depends on:** none

## Problem

No visual proof that Bernstein works. Every viral dev tool has a 15-second GIF in the README and a 2-minute YouTube video. Conductor, Dorothy, Parallel Code, Crystal ALL have visual demos. Bernstein's README has a static SVG. Nobody stars a repo they can't visualize working.

## Design

Create two assets:

### 1. README GIF (15 seconds)
Record a terminal session showing:
- `bernstein -g "Add auth, tests, and docs"`
- Live TUI dashboard with 3 agents spawning
- Tasks completing one by one (green checkmarks)
- Final: "3 tasks done, $0.42 spent, 47 seconds"

Use `vhs` (charmbracelet/vhs) or `asciinema` + `agg` for high-quality terminal recording. The GIF must autoplay in GitHub README.

### 2. YouTube video (2 min)
Same content but with voiceover:
- 0:00-0:15 "One command, multiple AI agents"
- 0:15-0:45 Show the full run with TUI
- 0:45-1:15 Show the git log, passing tests, actual code diff
- 1:15-1:45 Show cost breakdown
- 1:45-2:00 "Install: pipx install bernstein"

### 3. Update README
Replace static SVG dashboard with the autoplay GIF. Add YouTube badge link.

## Files to modify

- `docs/assets/demo.gif` (new)
- `README.md`

## Completion signal

- GIF exists and autoplays in README
- Demo shows real agent orchestration (not faked)
