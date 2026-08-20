# Web UI — token and component inventory

What the dashboard already uses, catalogued so a new screen can reuse the
vocabulary instead of inventing a parallel one. This describes `main` as it
stands; it is not a proposal.

Sources: `web/tailwind.config.js`, `web/src/index.css`, `web/src/components/`.

## Colour tokens

Declared as HSL triples in `web/src/index.css` and consumed through Tailwind,
so `bg-surface-raised` and `hsl(var(--surface-raised))` are the same value.
Light and dark are two sets of the same names — a component never branches on
theme.

| Group | Tokens |
|---|---|
| Base | `--background`, `--foreground` |
| Surfaces | `--card`, `--card-foreground`, `--popover`, `--popover-foreground`, `--surface-raised` |
| Emphasis | `--primary`, `--primary-foreground`, `--secondary`, `--secondary-foreground`, `--accent`, `--accent-foreground` |
| State | `--success`, `--success-foreground`, `--warning`, `--warning-foreground`, `--destructive`, `--destructive-foreground` |
| Recessive | `--muted`, `--muted-foreground`, `--meta-foreground` |
| Lines and focus | `--border`, `--border-subtle`, `--border-strong`, `--input`, `--ring` |
| Geometry | `--radius` (`lg` = the value, `md` = −2px, `sm` = −4px) |

`--meta-foreground`, `--surface-raised`, `--border-subtle`, `--border-strong`
and the `success` / `warning` pairs are additions on top of the shadcn base
set. They exist because this UI has three depths of line weight and a
metadata tier that the base set does not name.

### Contrast measurements

Derived dynamically from `web/src/index.css` tokens (HSL to relative luminance)
and gated in `tests/unit/test_webui_contrast.py` against WCAG AA (≥ 4.5:1 for body
text and pills). Solid backgrounds are measured directly; the `Pill` component's
tinted variants (`bg-{color}/15` in `web/src/lib/states.tsx`) are alpha-composited
over their backdrop first — the solid token is not what actually renders, and
measuring it as if it were understated the risk (see `warning` below).

| Token pair | Role / Component | Light ratio | Dark ratio | Standard |
|---|---|---|---|---|
| `--foreground` on `--background` | Page body text | 16.73:1 | 16.17:1 | WCAG AA (≥ 4.5:1) |
| `--foreground` on `--card` | Card titles and body | 17.74:1 | 15.18:1 | WCAG AA (≥ 4.5:1) |
| `--foreground` on `--surface-raised` | Text on raised containers | 17.00:1 | 13.87:1 | WCAG AA (≥ 4.5:1) |
| `--card-foreground` on `--card` | Card foreground text | 17.74:1 | 15.18:1 | WCAG AA (≥ 4.5:1) |
| `--popover-foreground` on `--popover` | Popover text | 17.74:1 | 14.38:1 | WCAG AA (≥ 4.5:1) |
| `--primary-foreground` on `--primary` | Primary action button | 16.73:1 | 16.17:1 | WCAG AA (≥ 4.5:1) |
| `--secondary-foreground` on `--secondary` | Secondary action button | 15.43:1 | 17.02:1 | WCAG AA (≥ 4.5:1) |
| `--accent-foreground` on `--accent` | Solid accent button / strong pill | 7.96:1 | 9.77:1 | WCAG AA (≥ 4.5:1) |
| `--destructive-foreground` on `--destructive` | Destructive action button | 7.81:1 | 7.38:1 | WCAG AA (≥ 4.5:1) |
| `--success-foreground` on `--success` | Solid success badge | 5.89:1 | 9.56:1 | WCAG AA (≥ 4.5:1) |
| `--warning-foreground` on `--warning` | Solid warning badge | 5.81:1 | 9.81:1 | WCAG AA (≥ 4.5:1) |
| `--muted-foreground` on `--background` | Recessive text on background | 6.87:1 | 6.87:1 | WCAG AA (≥ 4.5:1) |
| `--muted-foreground` on `--card` | Muted card description | 7.29:1 | 6.45:1 | WCAG AA (≥ 4.5:1) |
| `--muted-foreground` on `--surface-raised` | Default Pill / raised container text | 6.98:1 | 5.89:1 | WCAG AA (≥ 4.5:1) |
| `--meta-foreground` on `--background` | SectionLabel / uppercase metadata | 5.54:1 | 6.00:1 | WCAG AA (≥ 4.5:1) |
| `--meta-foreground` on `--card` | Card metadata / timestamp | 5.87:1 | 5.63:1 | WCAG AA (≥ 4.5:1) |
| `--meta-foreground` on `--surface-raised` | Raised metadata label | 5.62:1 | 5.15:1 | WCAG AA (≥ 4.5:1) |
| `--accent` on `--card` / `--surface-raised` (solid) | Interactive link / standalone accent text | 8.43:1 / 8.08:1 | 8.71:1 / 7.96:1 | WCAG AA (≥ 4.5:1) |
| `--destructive` on `--card` / `--surface-raised` (solid) | Standalone error text | 8.28:1 / 7.94:1 | 6.93:1 / 6.33:1 | WCAG AA (≥ 4.5:1) |
| `--success` on `--card` / `--surface-raised` (solid) | Standalone success text | 6.25:1 / 5.99:1 | 8.53:1 / 7.79:1 | WCAG AA (≥ 4.5:1) |
| `--warning` on `--card` / `--surface-raised` (solid) | Standalone warning text | 6.15:1 / 5.90:1 | 8.75:1 / 7.99:1 | WCAG AA (≥ 4.5:1) |
| `--accent` text on 15%-tint `bg-accent/15` over `--card` / `--surface-raised` | Accent Pill (non-strong) | 6.63:1 / 6.38:1 | 6.49:1 / 5.86:1 | WCAG AA (≥ 4.5:1) |
| `--destructive` text on 15%-tint `bg-destructive/15` over `--card` / `--surface-raised` | Danger Pill | 6.42:1 / 6.17:1 | 5.41:1 / 4.90:1 | WCAG AA (≥ 4.5:1) |
| `--success` text on 15%-tint `bg-success/15` over `--card` / `--surface-raised` | Success Pill | 5.04:1 / 4.85:1 | 6.38:1 / 5.76:1 | WCAG AA (≥ 4.5:1) |
| `--warning` text on 15%-tint `bg-warning/15` over `--card` / `--surface-raised` | Warning Pill | 4.97:1 / 4.78:1 | 6.50:1 / 5.87:1 | WCAG AA (≥ 4.5:1) |
| `--border-strong` on `--background` | Scrollbar thumb / high-contrast framing | 2.07:1 | 2.43:1 | Non-text UI |
| `--border` / `--border-subtle` on `--background` | Container dividers | 1.38:1 / 1.17:1 | 1.46:1 / 1.22:1 | Non-text UI |

The `warning` and `destructive` tokens were tuned (see comments in
`web/src/index.css`) once the tinted-Pill measurement above showed the
solid-token-only check was missing a real failure: light `warning` Pill text
measured 4.12:1 / 3.97:1 on `card` / `surface-raised` before the fix (below
4.5:1), and dark `destructive` Pill text on `surface-raised` measured 4.22:1.
Both solid pairings using the same tokens already passed, which is why the
gap existed — solid contrast is not a proxy for a 15%-opacity tint.


## Type scale

Two vendored variable fonts, no outbound request: `Inter Tight` (300–700) for
text, `JetBrains Mono` (400–600) for identifiers, hashes, and log output.
Provenance and hashes in `web/src/fonts/README.md`.

| Name | Size / line-height | Used for |
|---|---|---|
| `meta` | 11px / 1.2, tracking 0.12em | uppercase labels, column heads |
| `log` | 11.5px / 1.55 | log and trace output |
| `body` | 13px / 1.4 | default text |
| `body-md` | 14px / 1.35, 500 | emphasised row text |
| `h3` | 16px / 1.3, 600 | panel titles |
| `h2` | 20px / 1.2, 600 | section titles |
| `h1` | 30px / 1.05, 500 | page titles |
| `stat-md` | 18px / 1.15, 500 | inline figures |
| `stat-lg` | 24px / 1.1, 500 | headline figures |

Nine steps is already the full budget. A screen needing a tenth is usually a
screen using the wrong one of the nine.

## Motion

| Name | Definition | Used for |
|---|---|---|
| `fade-in` | 90ms `cubic-bezier(0.16, 1, 0.3, 1)` | content appearing in place |
| `drawer-in` | 250ms, 8px translate + fade | the task drawer |

Shared easing, two durations. `web/src/lib/motion.ts` holds the helpers.

## Component families

| Family | Location | Contents |
|---|---|---|
| Shell | `web/src/components/` | `AppShell`, `CommandPalette`, `ThemeProvider`, `SteeringControls`, `PlaceholderScreen` |
| Artifacts | `components/artifacts/` | `ArtifactCard`, `ProgressStrip`, `TaskArtifactsPanel`, plus its own `types.ts` and data hook |
| Dependencies | `components/deps/` | `TaskDepsPanel` |
| Diff | `components/diff/` | `DiffFileList`, `DiffFileView`, `DiffHeader`, `DiffLine`, `DiffStates`, `TaskDiffPanel`, `highlight.ts` |
| Gates | `components/gates/` | `TaskGatesPanel`, `GateRow`, `GateFilters`, `GateCountsHeader`, `GateStatusIcon`, `TaskLifecyclePill`, `buckets.ts`, `time.ts` |
| Logs | `components/logs/` | `TaskLogsPanel` plus twelve presentational parts (`LogLine`, `LogList`, `LogToolbar`, filters, follow/pause controls, keyboard help) and `ansi.ts`, `parseLine.ts`, four hooks |
| Trace | `components/trace/` | `TaskTracePanel`, `TraceEventCard`, `TraceFilters` |

The convention each family follows: a `Panel` component as the entry point,
narrow presentational children beside it, `types.ts` for the shape it renders,
and a `use*` hook for fetching. A new drawer tab that follows this shape needs
no new patterns.

## Shared modules

| Module | Responsibility |
|---|---|
| `lib/api.ts` | HTTP client for the orchestrator API |
| `lib/sse.ts` | server-sent-event stream handling |
| `lib/states.tsx` | `EmptyState`, `LoadingState`, `ErrorState`, plus `SectionLabel`, `StatusDot`, `Pill` |
| `lib/format.ts` | value formatting |
| `lib/motion.ts` | animation helpers |
| `lib/pwa.ts` | installability |
| `lib/utils.ts` | class-name merging and small helpers |

`lib/states.tsx` is the one to read before writing an empty state — principle
P2 in [web-ui-principles.md](web-ui-principles.md) is enforced by using it
rather than by hand-rolling per screen.

## Reference route

`/ui/vocabulary` is a developer-visible route kept out of operator navigation.
It renders the CSS custom properties available at runtime, the Tailwind type
scale, and the shared states from `lib/states.tsx` without making a request.

## Known gaps

Recorded here rather than in a comment nobody greps.

- None currently recorded. (The token contrast measurements previously noted
  here are documented above and gated in `tests/unit/test_webui_contrast.py`).
