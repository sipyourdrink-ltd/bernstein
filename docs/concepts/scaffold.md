# Prompt-to-repo scaffold

`bernstein scaffold "<prompt>"` materialises a small project
skeleton from a single goal prompt. A deterministic keyword
heuristic picks one template family from the registry, the template
is rendered to disk, and the generator returns the list of created
paths.

## Why it exists

Real project starts spend the first hour on the same scaffolding:
`README.md`, a stub entry point, a test directory, a license file,
a one-line CI config. None of that is interesting; all of it is
reproducible from a single sentence describing the goal.

The CLI ships the smallest viable slice of that workflow: pick a
template, render it, write to disk. The full goal-to-deploy flow
(architect, backend, frontend, reviewer; preview tunnel; deploy
adapter) composes on top of this primitive in follow-up slices.

## How to use it

```bash
# Auto-pick a template via keyword heuristic on the prompt
bernstein scaffold "Build me a habit tracker"

# Pin a specific template
bernstein scaffold "CLI to convert markdown to PDF" --template python-cli

# Custom output directory (defaults to ./<slug-of-prompt>)
bernstein scaffold "static landing page" --output ./my-site

# Force rendering into an existing directory
bernstein scaffold "Build a habit tracker" --output ./existing --force
```

Without `--force`, scaffold refuses a destination that already contains
anything; it does not write the template files there. With `--force`, each
path named by the selected template is written, replacing an existing file at
the same path. Other files already in the directory are left alone, not
removed.

For example, suppose `./existing` already contains a hand-written
`README.md` and a separate `notes.txt`:

```text
./existing/README.md   # your old readme
./existing/notes.txt   # unrelated notes
```

Running the forced command above replaces `README.md` with the generated
readme and leaves `notes.txt` unchanged. Without `--force`, the nonempty
directory is refused instead.

The default destination is derived from the prompt (for example, the prompt
"Build a habit tracker" defaults to `./build-a-habit-tracker`). The project
or package slug used inside generated files is derived separately from the
final destination directory name and sanitized. Thus with `--output
./my-site`, generated package names use `my-site`, not a slug of the prompt.

The output is a populated directory with a working entry point,
a `README.md` that paraphrases the prompt, and the next-step
command printed at the end:

```text
Scaffolded python-cli into /path/to/habit-tracker
  - README.md
  - pyproject.toml
  - src/habit-tracker/__init__.py
  - src/habit-tracker/main.py

Next: cd /path/to/habit-tracker && cat README.md
```

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `PROMPT` | required | The free-form prompt; drives `auto` template selection and the default destination name. |
| `--template NAME` | `auto` | One of the registered templates. `auto` runs the keyword heuristic. |
| `--output DIR` | `./<slug-of-prompt>` | Destination directory. The generated project/package slug is sanitized from the final destination directory name. |
| `--force` | off | Permit rendering into a non-empty destination; matching template paths are overwritten, unrelated files are preserved. |

Run `bernstein scaffold --help` to see the live template list. The current
built-in registry contains `python-cli`, `node-cli`, and `static-site`.

## Limitations

- The keyword heuristic is intentional but coarse. When the prompt
  matches several template families weakly, pin one with
  `--template NAME` rather than rely on `auto`.
- Templates are static. Variable substitution is limited to slug
  and prompt; richer parameterisation lives in follow-up slices.
- The generator does not create or initialize a Git repository, make a
  commit, or push to a remote. It writes files and returns; downstream
  tooling decides what to do with them.

## Related

- Source: `src/bernstein/cli/commands/scaffold_cmd.py`
- Templates: `src/bernstein/cli/scaffold/templates.py`
