# Quality Metrics Dashboard — Design Spec

**Date:** 2026-03-29
**Task:** 513a — Internal Quality Metrics Dashboard
**Status:** Approved (autonomous execution)

## Problem

The `/quality` and `/quality/models` API endpoints already return all required
metrics (success rate per model, avg tokens, guardrail pass rate, review
rejection rate, p50/p90/p99 completion times). However, none of this data
surfaces in the live Textual TUI (`bernstein live`). Operators tuning routing
or justifying costs must manually curl the endpoint.

## Solution

Add a `QualityPanel` widget to `src/bernstein/cli/dashboard.py` as a third
column in the `#top-panels` horizontal. The dashboard already uses two columns
(agents, tasks); quality becomes the third.

## Architecture

### Data Flow

```
_fetch_all()  →  _get("/quality")  →  quality: dict
_apply_data() →  _update_quality() →  QualityPanel.quality = data
QualityPanel.render()              →  Rich Text display
```

### `QualityPanel` widget

Static subclass with a single reactive attribute `quality: dict`. On change,
`render()` produces a Rich `Text` object showing:

- **QUALITY** header
- Overall success rate (color-coded green/yellow/red)
- Per-model table: model | success% | avg tokens | p50
- Guardrail pass rate (% of gate checks that didn't block/flag)
- Review rejection rate
- Completion time distribution: p50 / p90 / p99

### Layout changes

`#top-panels` gains a third Vertical column `#col-quality` (width 1fr).
Current agents and tasks columns keep their 1fr widths.

### CSS additions

```css
#col-quality {
    width: 1fr;
    border-left: heavy $border;
    padding: 0 1;
    overflow-y: auto;
}
```

### `_fetch_all` change

```python
"quality": _get("/quality"),
```

## What does NOT change

- `/quality` and `/quality/models` API routes — already correct
- `MetricsCollector.get_quality_metrics()` — already correct
- All existing tests — no modifications needed
- Dashboard polling interval (2s) — quality data refreshes with each poll

## Files changed

- `src/bernstein/cli/dashboard.py` — only file modified

## Out of scope

- Charting/sparklines for quality trends (future)
- Quality metrics in the VSCode extension (separate task)
