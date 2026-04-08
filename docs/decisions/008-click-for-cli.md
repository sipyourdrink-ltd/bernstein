# ADR-008: Click for the CLI

**Status**: Accepted  
**Date**: 2026-03-22  
**Context**: Bernstein multi-agent orchestration system

---

## Problem

Bernstein exposes user-facing commands: `bernstein run`, `bernstein status`,
`bernstein stop`, `bernstein agents`, `bernstein dashboard`, and more. These
need argument parsing, help text, subcommand nesting, and good error messages.

Python has several CLI frameworks. Which one do we use, and why?

---

## Decision

**Use [Click](https://click.palletsprojects.com/) (`click>=8.1`) as the CLI
framework.**

---

## Options evaluated

### Option A: `argparse` (stdlib)

```python
import argparse

parser = argparse.ArgumentParser(description='Bernstein orchestrator')
subparsers = parser.add_subparsers(dest='command')

run_parser = subparsers.add_parser('run', help='Start orchestration')
run_parser.add_argument('plan', nargs='?', help='Plan file')
run_parser.add_argument('--goal', help='High-level goal for planner')
run_parser.add_argument('--max-agents', type=int, default=3)
```

**Pros**: Zero dependencies. Everyone knows it.

**Cons**:
- Verbose. A simple subcommand with a few options requires 15–20 lines of
  boilerplate that does nothing except describe the interface.
- Help text quality is mediocre — single-line descriptions, no formatting.
- No automatic shell completion generation.
- Testing requires calling `sys.argv` manipulation or `parser.parse_args()` with
  string lists — awkward.
- No built-in support for environment variable overrides (`BERNSTEIN_MAX_AGENTS`
  automatically mapping to `--max-agents`).

**Verdict**: Sufficient for simple scripts; not suitable for a tool with 15+
subcommands that are the primary user interface.

### Option B: Typer

Typer builds on Click but uses Python type annotations as the command interface:

```python
import typer

app = typer.Typer()

@app.command()
def run(
    plan: Optional[str] = typer.Argument(None, help="Plan file path"),
    goal: Optional[str] = typer.Option(None, help="High-level goal"),
    max_agents: int = typer.Option(3, help="Maximum parallel agents"),
) -> None:
    """Start the Bernstein orchestrator."""
    ...
```

**Pros**:
- Annotation-driven — less boilerplate than raw Click.
- Generates rich help text automatically.
- Built on Click — full Click compatibility.

**Cons**:
- Adds a dependency (Typer) on top of Click — you still get Click as a transitive
  dep. If we're already depending on Click, Typer adds complexity without a clear
  benefit.
- Some Typer idioms (the automatic `Optional[str] = None` → optional argument
  pattern) generate surprising behavior for users who expect Click-style commands.
- Typer's `typer.Argument` / `typer.Option` with complex types can produce
  confusing errors.
- The Bernstein CLI has complex subcommand groups (`bernstein advanced`,
  `bernstein workspace`, etc.) where Click's explicit `@group.command()` pattern
  is clearer than Typer's nested apps.

**Verdict**: Nice for simple CLIs. The annotation-based approach doesn't save
much over raw Click for our command structure, and adds the cognitive overhead
of Typer-specific idioms.

### Option C: Click (chosen)

```python
import click

@click.group()
@click.version_option()
def cli() -> None:
    """Bernstein — multi-agent orchestration for CLI coding agents."""

@cli.command()
@click.argument('plan', required=False)
@click.option('--goal', help='High-level goal for the planner')
@click.option('--max-agents', default=3, show_default=True,
              envvar='BERNSTEIN_MAX_AGENTS', help='Maximum parallel agents')
def run(plan: str | None, goal: str | None, max_agents: int) -> None:
    """Start the orchestrator. Provide a PLAN file or --goal."""
    ...
```

**Why Click:**

1. **Decorator-driven interface matches Python conventions.** The `@cli.command()`
   pattern is idiomatic Python. Reading the CLI source tells you the interface
   without reading documentation.

2. **`envvar` support is built-in.** `envvar='BERNSTEIN_MAX_AGENTS'` makes every
   CLI option also settable via environment variable — essential for CI/CD usage
   where you don't want long command lines.

3. **Shell completion generation.** `bernstein --install-completion` (via
   click-autocomplete or the built-in mechanism) generates completions for bash,
   zsh, and fish. Users can tab-complete subcommands and option names.

4. **Testing with CliRunner.** Click's `CliRunner` makes testing CLI commands
   straightforward without sys.argv manipulation:
   ```python
   from click.testing import CliRunner
   
   def test_run_command():
       runner = CliRunner()
       result = runner.invoke(run, ['plans/test.yaml', '--max-agents', '2'])
       assert result.exit_code == 0
   ```

5. **Rich help text.** Multi-line docstrings in `@cli.command()` functions become
   well-formatted help text. `bernstein run --help` reads like documentation.

6. **Subcommand groups.** `@click.group()` / `@group.command()` pattern scales
   naturally to 15+ subcommands organized into groups (`bernstein advanced`,
   `bernstein workspace`, `bernstein cluster`).

7. **Battle-tested.** Click powers Flask, pip, dbt, Airflow's CLI, and thousands
   of other Python tools. Its semantics are stable and well-documented.

---

## CLI structure

```
bernstein
├── run         Start orchestration from a plan file or goal
├── stop        Stop running agents gracefully
├── status      Show current task and agent status
├── agents      List detected CLI agents
├── dashboard   Open the live TUI dashboard
├── evolve      Trigger a self-improvement run
├── add-task    Add a task to the backlog
├── workspace   Manage multi-repo workspaces
│   ├── add
│   ├── list
│   └── remove
└── advanced    Advanced orchestration controls
    ├── approve-upgrade
    ├── force-requeue
    └── set-policy
```

Subcommand groups (`workspace`, `advanced`) use `@click.group()` to avoid
polluting the top-level namespace with infrequently used commands.

---

## Consequences

### Benefits

**Consistent `--help` output.** Every command and option has help text. Users
can discover the interface without reading docs.

**Environment variable support.** CI/CD pipelines set `BERNSTEIN_MAX_AGENTS=5`
instead of passing `--max-agents 5` on every command.

**Testable CLI.** `CliRunner` makes unit testing CLI commands fast and reliable.
The CLI test suite runs without spawning real agents.

**Shell completion.** Tab completion reduces friction for daily use.

### Costs

**One dependency.** Click is a non-stdlib dependency. It's stable and widely
used, so the risk is low.

**Click semantics aren't universal.** A developer who doesn't know Click needs to
learn that `@click.option` with a default doesn't create a required argument,
that `required=False` on `@click.argument` changes the argument to optional, etc.
These are Click idioms, not universal Python. The Click docs are comprehensive,
so this is an acceptable learning curve.

---

## References

- Implementation: `src/bernstein/cli/`
- Click docs: https://click.palletsprojects.com/
