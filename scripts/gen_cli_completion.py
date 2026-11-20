"""Regenerate lightweight root-completion metadata from the registered Click commands."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.main import cli


def main() -> None:
    metadata = {
        name: None if command.hidden else command.get_short_help_str() for name, command in sorted(cli.commands.items())
    }
    output = Path(__file__).resolve().parents[1] / "src" / "bernstein" / "cli" / "completion_metadata.json"
    output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
