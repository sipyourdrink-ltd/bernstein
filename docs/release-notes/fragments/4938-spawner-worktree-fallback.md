### Fixed

- When a per-session worktree cannot be created, the spawner now logs a warning and falls back to the main working directory instead of raising SpawnError. This allows orchestration to continue when worktree creation fails transiently. ([#4938](https://github.com/sipyourdrink-ltd/bernstein/issues/4938))