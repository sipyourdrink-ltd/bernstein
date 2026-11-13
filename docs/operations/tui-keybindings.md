# TUI keybindings

The Bernstein TUI's keyboard shortcuts are configurable. Defaults ship
built in; a project can override them in `bernstein.yaml`, and an operator
can override them again per-machine in a JSON file, without touching TUI
code.

## Resolution order

Bindings resolve in three layers, highest priority last:

1. **Defaults** — `DEFAULT_BINDINGS` (`src/bernstein/cli/keybindings.py`)
   plus `EXTENDED_BINDINGS` (`src/bernstein/tui/keybinding_config.py`).
2. **Project YAML** — the first existing file found is selected: `bernstein.yaml`
   in the current directory, then `~/.bernstein/bernstein.yaml`. The selected
   file is used as a whole; if it has no `keybindings:` section, the YAML
   override layer is empty and the loader does not fall back to the other file.
3. **User JSON** — `~/.bernstein/keybindings.json`, applied last, so it
   wins over both defaults and the project YAML.

For example, in `bernstein.yaml`:

```yaml
keybindings:
  quit: "Q"
  refresh: "F5"
```

And in `~/.bernstein/keybindings.json`:

```json
{
  "quit": "X"
}
```

The resolved `quit` binding is `X` from JSON, while `refresh` remains `F5`
from YAML. Each layer only overrides actions it names; every other known
action keeps its binding from the preceding layer. Overrides for action names
the resolver does not know are ignored.

Each layer maps an **action name** (e.g. `quit`, `toggle_split_pane`) to a
**key combination** (e.g. `"Q"`, `"ctrl+l"`). An override only replaces
the key for that one action; every action without an override keeps its
previous binding (or its default if no earlier layer overrides it).

## Reserved keys

`ctrl+c` and `ctrl+d` can never be rebound. If an override requests either
reserved key, it is skipped, the action keeps its binding from the preceding
layer (or its default), and a warning is logged. This applies to YAML and JSON
overrides.

## How it's wired into the TUI

The Textual app builds its `BINDINGS` class variable at import time by
calling `resolve_all_bindings()`, which merges the three layers above:

```python
# src/bernstein/tui/app.py
from bernstein.tui.keybinding_config import resolve_all_bindings as _resolve_all_bindings


def _build_app_bindings() -> list[BindingType]:
    bindings = [
        Binding(e.key, e.action, e.description, show=e.show, priority=e.priority) for e in _resolve_all_bindings()
    ]
    ...
```

Pressing `?` or `h` opens the in-TUI help screen, which also calls
`resolve_all_bindings()` and renders the live, fully-resolved key map —
so the help screen always reflects your current overrides, not just the
defaults.

## Limitations

A handful of App-level bindings are registered directly in
`src/bernstein/tui/app.py` (task search `/`, notification-ack `n`,
session-recording toggle `R`, and the layout-preset digits `1`/`2`/`3`)
outside the `resolve_all_bindings()` merge. These are not looked up
through the keybinding resolution system, so they cannot currently be
remapped via `bernstein.yaml` or `keybindings.json`.

## Source

- `src/bernstein/cli/keybindings.py` — default bindings, reserved-key
  check, and `~/.bernstein/keybindings.json` loading.
- `src/bernstein/tui/keybinding_config.py` — extended defaults,
  `bernstein.yaml` `keybindings:` loading, and layered resolution
  (`resolve_all_bindings`).
- `src/bernstein/tui/app.py` — builds the Textual `BINDINGS` list from
  `resolve_all_bindings()` at startup.
- `src/bernstein/tui/help_screen.py` — renders the live resolved key map.
