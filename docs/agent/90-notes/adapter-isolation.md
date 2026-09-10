# Adapter & Spawner Isolation Architecture

Bernstein governs external agent CLI processes through uniform adapter boundaries.

## Isolation Principles
1. **Subprocess Boundary**: Agent CLIs run as child processes under adapter lifecycle management defined in `src/bernstein/adapters/base.py:69`.
2. **Host Isolation**: Workspace isolation and gitdir segregation are handled by `src/bernstein/core/agents/spawner_worktree.py:36`.
3. **Execution Guard**: Spawn errors and containment failures raise `src/bernstein/adapters/base.py:69`.
