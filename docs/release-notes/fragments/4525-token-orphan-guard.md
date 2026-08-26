---
`core/tokens/` no longer accumulates modules with no caller
---

- Three modules with no runtime caller and no reachable path are removed together with their test suites and their `bernstein/core/__init__.py` alias entries: `auto_distillation`, `cascading_token_counter`, `token_waste_report`.
- A structural test now fails CI when a new caller-less module appears under `core/tokens/`. Reachability resolves the compat redirect map, so a module imported only through its legacy `bernstein.core.<name>` path is correctly seen as live, and it is computed from callers outside the package so a pair of dead modules importing each other no longer vouches for itself.
