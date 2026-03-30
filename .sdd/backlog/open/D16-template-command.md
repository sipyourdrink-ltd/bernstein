# D16 — Template Command for Scaffolding Configs

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
Users who discover example configs still have to manually copy files into their project. There's no built-in way to browse and apply templates from the command line.

## Solution
- Implement `bernstein template list` that reads the `examples/` directory and displays available templates in a formatted table: name, description (from README first line), stack.
- Implement `bernstein template use <name>` that copies the template's `bernstein.yaml` into the current working directory.
- If a `bernstein.yaml` already exists, prompt: "bernstein.yaml already exists. Overwrite? [y/N]".
- Support `--force` flag to skip the overwrite prompt.
- Templates are sourced from the bundled `examples/` directory shipped with the package.

## Acceptance
- [ ] `bernstein template list` displays all available templates with names and descriptions
- [ ] `bernstein template use fastapi-crud` copies the correct `bernstein.yaml` to the current directory
- [ ] Attempting to overwrite an existing `bernstein.yaml` prompts for confirmation
- [ ] `--force` flag bypasses the overwrite prompt
- [ ] Using a non-existent template name shows an error with a list of valid templates
