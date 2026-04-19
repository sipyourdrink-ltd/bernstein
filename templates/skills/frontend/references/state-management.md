# State management patterns

## Hierarchy
1. **URL** — shareable, reloadable state (filters, pagination).
2. **Server** — persistent data (React Query / tRPC cache).
3. **Local component** — `useState`, `useReducer` for ephemeral UI state.
4. **Context** — cross-component but scoped; avoid as a global store.
5. **Global store** (Zustand, Redux Toolkit) — rare, last resort.

## Rules of thumb
- Lift state only as high as needed.
- Keep derived values derived; do not duplicate in state.
- Prefer controlled components for forms.
- Memoize only after measuring; premature memoization hides bugs.
- Async state belongs to a data-fetching library — do not reinvent
  cancellation, staleness, and retry.
