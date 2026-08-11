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

## Known gaps

Recorded here rather than in a comment nobody greps.

- The token set has no documented contrast measurements. `success` and
  `warning` on `surface-raised` are the pairs most likely to be short.
- No route renders a design-system reference page, so the only way to see the
  vocabulary is to read this file next to the code.
