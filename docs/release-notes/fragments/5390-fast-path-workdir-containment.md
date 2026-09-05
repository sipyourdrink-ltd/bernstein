## Constrain fast_path ruff execution to workdir and guard against system tempdir

Fast-path deterministic L0 executors (`_run_ruff_format`, `_run_ruff_fix`, `_run_sort_imports`)
now validate that target `owned_files` are contained within the task's `workdir` and refuse
unbounded project-wide execution (`targets=["."]`) when `workdir` is a filesystem root or
system temporary directory (/tmp, /var/tmp, tempfile.gettempdir()). This prevents tests or
unbounded runs from mutating external repositories (#5390).
