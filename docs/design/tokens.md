# Design tokens

Three surfaces carry three token sets — the website, the browser operator UI
under `web/`, and the terminal UI under `src/bernstein/tui/` — and nothing said
which value wins when two disagree. This page is that statement: the canonical
value per semantic name, the source file it lives in, and the measured contrast
for every text-on-background pair. A token that does not exist is recorded as
not defined rather than filled in with a neighbour.

## Decision — the canonical accent (2026-09-02)

The canonical accent is the website's terracotta, `oklch(55% 0.10 35)` /
`#A35B48`. The operator UI keeps its deep teal until it migrates, tracked
separately. Light values below come from the website, dark from the operator
UI. Where the two disagree on a non-accent token, dark takes the operator UI's
value — gated against WCAG AA by `tests/unit/test_webui_contrast.py` — and
light takes the website's, the surface they were tuned on.

## Colour tokens

| Semantic | Light source | Light hex | Dark source | Dark hex |
|---|---|---|---|---|
| `bg` | `oklch(96% 0.015 75)` | `#F8F1E7` | `hsl(60 11.8% 6.7%)` | `#13130F` |
| `surface` | `oklch(94% 0.02 75)` | `#F3EADD` | `hsl(60 10.6% 9.2%)` | `#1A1A15` |
| `text` | `oklch(20% 0.005 60)` | `#181614` | `hsl(45 31.6% 92.5%)` | `#F2EFE6` |
| `muted` | `oklch(45% 0.005 60)` | `#575552` | `hsl(44.3 11.1% 59.4%)` | `#A39D8C` |
| `border` | `oklch(85% 0.01 75)` | `#D2CDC7` | `hsl(60 8.5% 18.4%)` | `#33332B` |
| `accent` | `oklch(55% 0.10 35)` | `#A35B48` | `hsl(175.1 38.2% 62.5%)` | `#7BC4BE` |
| `success` | `oklch(45% 0.08 145)` | `#376139` | `hsl(129.4 37.6% 63.5%)` | `#7FC58A` |
| `warning` | `oklch(55% 0.10 60)` | `#9C622F` | `hsl(38.7 59.1% 63.5%)` | `#D9B26B` |
| `danger` | `oklch(50% 0.12 25)` | `#9C433F` | `hsl(6.5 65.9% 70%)` | `#E58B80` |
| `info` | not defined | — | not defined | — |

The dark `accent` row is the operator UI's current value, not the canonical
one; both are recorded so the migration has a before and an after. No surface
defines `info` — the terminal UI's `secondary` is the only role that behaves
like one, and a screen needing one today reuses `accent` or `muted`.

### Contrast, text on `bg`

Computed from the values above by the sRGB relative-luminance formula. WCAG AA
is 4.5:1 for normal text, 3:1 for large text; AAA is 7:1.

| Pair | Light | Level | Dark | Level |
|---|---|---|---|---|
| `text` on `bg` | 16.11:1 | AAA | 16.17:1 | AAA |
| `muted` on `bg` | 6.62:1 | AA | 6.87:1 | AA |
| `accent` on `bg` | 4.51:1 | AA | 9.28:1 | AAA |
| `success` on `bg` | 6.38:1 | AA | 9.08:1 | AAA |
| `warning` on `bg` | 4.44:1 | AA large only | 9.32:1 | AAA |
| `danger` on `bg` | 5.67:1 | AA | 7.38:1 | AAA |

Two results constrain use. Light `warning` measures 4.44:1, under the body-text
threshold — large text, icons, and fills only. The canonical accent measures
4.51:1 on `bg` but 4.25:1 on `surface`, so accent body text belongs on `bg`,
not on a raised card. Operator UI pairs including tinted pills, where a
15%-opacity fill composites to a different ratio than the solid token, are in
[web-ui-inventory.md](web-ui-inventory.md) and gated in CI.

## Type ramp

Fourteen steps across three families, from `tailwind.config.ts` in the website
repository. Each step ships size, leading, tracking, and weight together.

| Step | Size | Leading | Tracking | Weight |
|---|---|---|---|---|
| `display-1` | `clamp(3rem, 7vw, 5rem)` | 0.95 | −0.035em | 400 |
| `display-2` | `clamp(2rem, 4vw, 3.25rem)` | 1.05 | −0.025em | 400 |
| `display-3` | `clamp(1.5rem, 3vw, 2.25rem)` | 1.12 | −0.018em | 400 |
| `title-1` | 1.375rem | 1.2 | −0.014em | 400 |
| `title-2` | 1.125rem | 1.25 | −0.01em | 400 |
| `body-lg` | 1.0625rem | 1.55 | −0.003em | inherit |
| `body` | 0.9375rem | 1.55 | −0.002em | inherit |
| `body-sm` | 0.8125rem | 1.5 | 0 | inherit |
| `ui-lg` | 0.9375rem | 1.3 | −0.005em | 500 |
| `ui` | 0.8125rem | 1.35 | −0.003em | 500 |
| `mono-eyebrow` | 0.6875rem | 1.35 | 0.14em | 500 |
| `mono-stat` | 0.8125rem | 1.2 | −0.01em | 500 |
| `mono-tag` | 0.6875rem | 1.3 | 0.02em | 400 |
| `mono-kbd` | 0.75rem | 1 | 0.02em | 500 |

The operator UI runs a separate nine-step ramp in `web/src/lib/type-scale.js`,
11px to 30px, tabulated in [web-ui-inventory.md](web-ui-inventory.md). It is
not a subset of the fourteen and is not being merged into them — the two
surfaces set text at different densities.

| Surface | Sans | Serif | Mono |
|---|---|---|---|
| Website | Inter, `-apple-system`, `system-ui` | Fraunces, Iowan Old Style, Charter, Georgia | JetBrains Mono, SF Mono, Fira Code |
| Operator UI | Inter Tight 300–700 | none | JetBrains Mono 400–600 |
| Terminal UI | the terminal's own font | — | the terminal's own font |

The operator UI vendors both families under `web/src/fonts/` and loads no
outbound stylesheet, so the dashboard renders with no network request.

## Spacing and radii

An 8px grid with one half-step below it, from `styles/globals.css` in the
website repository.

| `--space-*` | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| value | 4px | 8px | 12px | 16px | 24px | 32px | 48px | 64px | 96px | 128px |

`--space-11` and `--space-12` are aliases of 96px and 128px kept for older
components; new work uses `--space-9` and `--space-10`.

| Radius | Website | Operator UI |
|---|---|---|
| `sm` | — | 2px (`calc(var(--radius) - 4px)`) |
| `md` | — | 4px (`calc(var(--radius) - 2px)`) |
| base / `lg` | 8px `--radius`, 12px `--radius-lg` | 6px `--radius` |

The two bases differ by 2px. Neither is wrong; the operator UI sits denser.

## Shadows

Five steps, website only, tinted with the text colour rather than pure black.

| Token | Value |
|---|---|
| `--shadow-xs` | `0 1px 0 0 oklch(20% 0.005 60 / 0.04)` |
| `--shadow-sm` | `0 1px 2px oklch(20% 0.005 60 / 0.06)` |
| `--shadow-md` | `0 2px 6px oklch(20% 0.005 60 / 0.08)` |
| `--shadow-lg` | `0 6px 16px oklch(20% 0.005 60 / 0.10)` |
| `--shadow-xl` | `0 12px 32px oklch(20% 0.005 60 / 0.12)` |

The operator UI declares no shadow scale. It separates depths with line weight
— `--border-subtle`, `--border`, `--border-strong` — over `--surface-raised`.

## Terminal UI

The terminal palette holds to four semantic colours plus one accent; everything
else is background, foreground, or a line. Values from
`src/bernstein/tui/themes.py`, with contrast against that theme's background.

| Semantic | Field in `themes.py` | Dark | Ratio | Light | Ratio |
|---|---|---|---|---|---|
| `accent` | `primary` | `#89B4FA` | 7.79:1 | `#1E66F5` | 4.34:1 |
| `success` | `success` | `#A6E3A1` | 11.03:1 | `#40A02B` | 2.96:1 |
| `warning` | `warning` | `#F9E2AF` | 12.91:1 | `#DF8E1D` | 2.31:1 |
| `danger` | `error` | `#F38BA8` | 7.08:1 | `#D20F39` | 4.80:1 |
| `muted` | `muted` | `#6C7086` | 3.36:1 | `#9CA0B0` | 2.30:1 |
| `text` | `foreground` | `#CDD6F4` | 11.34:1 | `#4C4F69` | 7.06:1 |
| `bg` | `background` | `#1E1E2E` | — | `#EFF1F5` | — |

The `status_*` fields add no colours: `status_running` and `status_done` hold
`success`, `status_failed` holds `error`, `status_pending` holds `foreground`,
and `generate_theme_css` paints blocked and cancelled rows with `muted`.
`border`, `selection`, and `secondary` are the three values outside the rule.

Four of the light theme's ratios fall under 4.5:1 and three under 3:1. That
theme is not WCAG-audited; `high_contrast` in the same file is the accessible
variant, selected with `BERNSTEIN_THEME=high_contrast`.

## Voice

- Sentence case in headings. Only the first word and proper nouns take a
  capital.
- Concrete nouns. `--border-strong` is a line weight, not a visual treatment.
- Numbers instead of adjectives: "4.51:1", "8px", "fourteen steps" — never
  "high contrast" or "generous spacing".
- Name the gap. A token that does not exist is written as not defined, not
  quietly filled with the nearest neighbour.
- No superlatives and no persuasion. State the value; the reader decides.

## How to change a token

Edit the source file for the surface, then update this page in the same change
so the two do not drift.

| Surface | Source file | Holds |
|---|---|---|
| Website | `styles/globals.css` in the website repository | light palette, spacing, radii, shadows, font stacks |
| Operator UI | `web/src/index.css` | both themes; type ramp in `web/src/lib/type-scale.js` |
| Terminal UI | `src/bernstein/tui/themes.py` | all three terminal themes |

A colour change to `web/src/index.css` also has to pass
`tests/unit/test_webui_contrast.py`, which recomputes every pair from the file
and fails under WCAG AA. The other two sources have no automated contrast gate:
measure by hand and put the ratio in the table above. `tokens.json` beside this
file carries the same colour, spacing, and radius values in machine-readable
form, maintained by hand from the same sources.
