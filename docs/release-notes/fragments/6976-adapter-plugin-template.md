## The adapter cookiecutter template generates a working adapter

`templates/adapter_plugin` generated an adapter that raised `TypeError`
when it was constructed and again when it was spawned. The template
called `CLIAdapter.__init__` with arguments it does not take, declared a
positional `spawn(task_description, ...)`, and returned a `SpawnResult`
with fields that do not exist. The generated adapter now has the current
keyword-only `spawn()` signature, launches the CLI without waiting for
it, and returns `SpawnResult(pid, log_path, proc)`. Its generated test
file passes. `docs/adapters/ADAPTER_GUIDE.md` describes the template.
