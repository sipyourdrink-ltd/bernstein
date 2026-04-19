# Decomposition principles

## Module boundaries
- Cohesion inside, loose coupling outside.
- A module's public API is a deliberate surface, not an accident of
  which symbols happen to be importable.
- Keep I/O at the edges. Core logic should be pure and testable without
  mocking a network stack.

## Package layout (Bernstein)
- `src/bernstein/core/<domain>/` — one domain per sub-package (orchestration,
  agents, tasks, quality, server, …).
- Cross-package imports travel through explicit public interfaces, not
  internal helpers.
- Circular imports are a design smell — refactor, don't paper over with
  lazy imports.

## Interface design
- Prefer small, stable interfaces over large, changing ones.
- Protocol / ABC for points of extension (adapters, skill sources).
- Versioned schemas for anything crossing a process boundary.

## Change tolerance
- A design is only as good as how gracefully it breaks.
- Optional parameters should always have safe defaults.
- Kill features with a deprecation cycle, not a silent removal.
