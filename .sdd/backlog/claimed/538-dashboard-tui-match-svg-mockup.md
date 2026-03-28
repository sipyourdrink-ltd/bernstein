# 538 — Dashboard TUI: make real interface match SVG mockup exactly

**Role:** frontend
**Priority:** 1 (critical)
**Scope:** medium

## Problem

The README dashboard SVG (`docs/assets/dashboard.svg`) shows a polished,
information-dense Bloomberg-terminal-style interface. The real TUI
(`src/bernstein/cli/dashboard.py`) is functionally correct but visually
much sparser — the first impression for anyone reading the README is
a lie. Every visual promise in the SVG must exist in the real TUI.

## SVG vs TUI: element-by-element diff

### 1. Agent status dot colors — WRONG

**SVG**: working = green (#3fb950 ◉), starting = yellow (#d29922 ◎)
**TUI code** (line 107-108):
```python
color = {
    "working": "bright_yellow",   # ← WRONG, SVG says green
    "starting": "bright_cyan",    # ← WRONG, SVG says yellow
    ...
}
```

**Fix**: Change to:
```python
color = {
    "working": "bright_green",    # match SVG green ◉
    "starting": "bright_yellow",  # match SVG yellow ◎
    "dead": "bright_red",
}
```

### 2. Agent log tail count — TOO FEW

**SVG**: Shows 1 task line (→) + 3-4 log lines per agent = 4-5 visible lines
**TUI code** (line 129): `_tail_log(aid, 3)` = only 3 log lines

**Fix**: Change to `_tail_log(aid, 4)` or `_tail_log(aid, 5)` and increase
`AgentWidget` max-height from 12 to 14:
```css
AgentWidget {
    max-height: 14;  /* was 12 */
}
```

### 3. Agent divider lines — MISSING

**SVG** (lines 125, 153): Shows horizontal divider lines between agents:
```svg
<line x1="16" y1="163" x2="386" y2="163" stroke-width="0.5"
      class="divider-line" opacity="0.5"/>
```

**TUI**: No dividers between agents. Only `margin: 0 0 1 0`.

**Fix**: Add a bottom border to AgentWidget:
```css
AgentWidget {
    border-bottom: solid $border;
    padding-bottom: 1;
}
```

OR add a Rule widget between agents in `_update_agents()`.

### 4. Task table column alignment — MISALIGNED

**SVG** (line 184): Shows aligned columns with fixed-width role names:
```
 ✓  BACKEND   Parse goal into task graph
 ✓  INFRA     Set up .sdd/ state directory
 ⚡  QA        Write CLI integration tests
```
Roles are right-padded to 8 chars, task text starts at fixed column.

**TUI code** (lines 451-459): Uses DataTable which auto-sizes columns.
Role column may be narrow if all roles are short.

**Fix**: Pad role names in `_update_tasks()`:
```python
Text(t.get("role", "-").upper().ljust(9), style=color),
```

### 5. Activity bar role colors — WRONG

**SVG** (lines 242-266): Each role name is colored by that role's theme:
- BACKEND → green (text-accent)
- QA → green (text-accent)
- SECURITY → yellow (text-yellow)

**TUI code** (line 493): All roles rendered identically as `[bold]{role}[/]`
which just bolds them without role-specific coloring.

**Fix**: Color-code roles in `_update_activity()`:
```python
ROLE_COLORS = {
    "backend": "bright_green",
    "frontend": "bright_cyan",
    "qa": "bright_green",
    "security": "bright_yellow",
    "devops": "bright_cyan",
    "architect": "bright_magenta",
    "manager": "bright_white",
    "docs": "bright_blue",
}
# ...
color = ROLE_COLORS.get(role.lower(), "bright_white")
new_lines.append(f"[bold {color}]{role.upper()}[/] {clean}")
```

### 6. Activity bar text color — WRONG

**SVG**: Activity message text uses `text-primary` (bright text), not dim.
Except for older entries which use `text-dim`.

**TUI**: All messages inherit dim from `{clean}` without explicit styling.

**Fix**: Use primary text color for messages:
```python
new_lines.append(f"[bold {color}]{role.upper()}[/] []{clean}")
```
(Remove dim styling — Textual default text color is fine.)

### 7. BigStats: evolve marker — SLIGHTLY OFF

**SVG** (lines 276-279): Dark teal box (#1a4d4d) with cyan ∞ (#00d7d7)
**TUI** (line 157): `style="bold white on dark_cyan"` — close but white text
vs SVG's specific cyan text.

**Fix**:
```python
t.append(" \u221e ", style="bold bright_cyan on rgb(26,77,77)")
```

### 8. BigStats: agent count color — MATCHES (mostly)

**SVG**: cyan (#58a6ff dark / #0550ae light) for "3 agents"
**TUI**: `bright_cyan` — acceptable, no change needed.

### 9. Sparkline position and sizing — CHECK

**SVG** (line 317-319): Sparkline at y=456, 14 characters, accent color
**TUI**: Sparkline widget in `#spark-row`, height 2, using `max` summary

This should match visually. Verify at runtime.

### 10. Chat input border — SLIGHTLY OFF

**SVG** (lines 322-326): Simple rect border, consistent with divider-line color.
**TUI CSS** (lines 296-303):
```css
ChatInput { border: tall $surface; }
ChatInput:focus { border: tall $accent; }
```

**Fix**: Change unfocused border to be visible:
```css
ChatInput {
    border: tall $border;  /* was $surface — invisible */
}
```

### 11. Footer key styling — TEXTUAL DEFAULT

**SVG** (lines 334-377): Each key has a colored background box:
- Green bg (#1a4d1a dark) with green text (#3fb950 dark)
- Green bg (#1a7f37 light) with white text (light mode)

**TUI**: Uses Textual's `Footer()` widget which renders its own key styling.
This is mostly acceptable but may not match the exact green palette.

**Fix**: Override Footer CSS:
```css
Footer {
    background: $surface;
}
Footer > .footer--key {
    background: $accent 30%;
    color: $accent;
}
```

### 12. Column proportions — CHECK AT RUNTIME

**SVG**: Agents column and Tasks column are 50/50 split (400px divider at
800px total). Activity bar spans full width. Bottom bar spans full width.

**TUI CSS**:
```css
#col-agents { width: 1fr; }
#col-tasks { width: 1fr; }
```

This is correct (50/50). Verify at runtime.

### 13. Top panels vs activity bar height ratio — MAY NEED TUNING

**SVG**: Top panels (agents+tasks) take ~60% of vertical space, activity
takes ~20%, bottom bar ~20%.

**TUI CSS**:
```css
#top-panels { height: 2fr; }
#activity-bar { height: 1fr; }
```

This gives 2:1 ratio (67% vs 33%). SVG shows more like 3:1.

**Fix**: Change to `3fr` / `1fr` or make activity bar `max-height`:
```css
#top-panels { height: 3fr; }
#activity-bar { height: 1fr; max-height: 8; }
```

### 14. Header bar background — CHECK

**SVG** (line 74): Uses `hdr-bg` class:
- Dark: `#0d1f0d` (very dark green)
- Light: `#1a7f37` (green)

**TUI CSS** (lines 217-219):
```css
Header {
    color: $accent;
    text-style: bold;
}
```

Textual's Header uses `$accent` for color but may not have green bg.

**Fix**: Add explicit background:
```css
Header {
    background: $accent 15%;
    color: $accent;
    text-style: bold;
}
```

## Summary of all changes

| # | Element | Issue | File | Line(s) |
|---|---------|-------|------|---------|
| 1 | Agent dot colors | green/yellow swapped | dashboard.py | 107-108 |
| 2 | Agent log lines | 3 → 4-5 | dashboard.py | 129 |
| 3 | Agent dividers | missing horizontal lines | dashboard.py CSS | 252-257 |
| 4 | Task role padding | not fixed-width | dashboard.py | 457 |
| 5 | Activity role colors | no role-specific coloring | dashboard.py | 493 |
| 6 | Activity message color | too dim | dashboard.py | 493 |
| 7 | Evolve marker | white → cyan text | dashboard.py | 157 |
| 8 | Chat input border | invisible unfocused | dashboard.py CSS | 296-300 |
| 9 | Footer key colors | may not match green | dashboard.py CSS | after 311 |
| 10 | Panel height ratio | 2:1 → 3:1 | dashboard.py CSS | 221, 237 |
| 11 | Header bg | no green tint | dashboard.py CSS | 216-219 |
| 12 | AgentWidget height | 12 → 14 | dashboard.py CSS | 254 |

## Files to modify
- `src/bernstein/cli/dashboard.py` — all changes above (CSS block + Python rendering logic)

## Testing
- Run `bernstein live` with at least 3 active agents
- Screenshot the TUI
- Compare side-by-side with the SVG (open both in browser)
- Iterate until they are visually indistinguishable at a glance
- Test both light and dark terminal themes

## Completion signal
- Side-by-side screenshot of TUI and SVG looks like the same interface
- All 12 items above are addressed
- No regressions in functionality (chat, keybindings, polling, stop)
