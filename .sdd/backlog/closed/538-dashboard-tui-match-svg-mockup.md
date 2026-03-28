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

### 8. BigStats: agent count color — MATCHES

**SVG**: cyan (#58a6ff dark / #0550ae light) for "3 agents"
**TUI**: `bright_cyan` — acceptable match, no change needed.

### 9. Chat input border — INVISIBLE WHEN UNFOCUSED

**SVG** (lines 322-326): Simple rect border, visible at all times.
**TUI CSS** (lines 296-300):
```css
ChatInput { border: tall $surface; }  /* $surface ≈ invisible */
```

**Fix**: Make unfocused border visible:
```css
ChatInput {
    border: tall $border;  /* was $surface */
}
```

### 10. Footer key styling — NEEDS GREEN TINT

**SVG** (lines 334-377): Each key has a green background box:
- Dark: bg #1a4d1a, text #3fb950
- Light: bg #1a7f37, text white

**TUI**: Uses Textual `Footer()` widget with default styling.

**Fix**: Override Footer CSS to match green palette:
```css
Footer {
    background: $surface;
}
Footer > .footer--key {
    background: $accent 30%;
    color: $accent;
}
```

### 11. Panel height ratio — WRONG PROPORTIONS

**SVG**: Top panels ≈ 60% height, activity ≈ 17%, bottom bar ≈ 23%.
**TUI CSS**:
```css
#top-panels { height: 2fr; }   /* = 67% */
#activity-bar { height: 1fr; } /* = 33% — too much */
```

**Fix**: Change to match SVG proportions:
```css
#top-panels { height: 3fr; }
#activity-bar { height: 1fr; max-height: 8; }
```

### 12. Header bar background — NO GREEN TINT

**SVG** (line 74): Header bar has green background:
- Dark: `#0d1f0d` (very dark green)
- Light: `#1a7f37` (solid green)

**TUI CSS**: No explicit background on Header.

**Fix**:
```css
Header {
    background: $accent 15%;
    color: $accent;
    text-style: bold;
}
```

## Summary: all 12 changes

| # | Element | Issue | Fix location |
|---|---------|-------|-------------|
| 1 | Agent dot colors | green/yellow swapped | `dashboard.py:107` |
| 2 | Agent log lines | 3 → 4+ lines | `dashboard.py:129`, CSS |
| 3 | Agent dividers | missing horizontal lines | CSS `AgentWidget` |
| 4 | Task role padding | not fixed-width | `dashboard.py:457` |
| 5 | Activity role colors | no role-specific coloring | `dashboard.py:493` |
| 6 | Activity message text | too dim | `dashboard.py:493` |
| 7 | Evolve marker color | white → cyan text | `dashboard.py:157` |
| 8 | Chat input border | invisible unfocused | CSS `ChatInput` |
| 9 | Footer key colors | no green tint | CSS `Footer` |
| 10 | Panel height ratio | 2:1 → 3:1 | CSS `#top-panels` |
| 11 | Header background | no green tint | CSS `Header` |
| 12 | AgentWidget height | 12 → 14 | CSS `AgentWidget` |

## Files to modify
- `src/bernstein/cli/dashboard.py` — all changes (single file: Python + CSS)

## Testing
1. Run `bernstein live` with at least 3 active agents
2. Screenshot the TUI
3. Open `docs/assets/dashboard.svg` in browser
4. Compare side-by-side — they must be visually indistinguishable at a glance
5. Test in both light and dark terminal themes
6. Verify no regressions: chat input, keybindings, polling, stop, activity toggle

## Completion signal
- Side-by-side screenshot of TUI and SVG looks like the same interface
- All 12 items above addressed
- No regressions in functionality


---
**completed**: 2026-03-28 11:32:29
**task_id**: 60698839a53d
**result**: Completed: 538 — Dashboard TUI: make real interface match SVG mockup exactly
