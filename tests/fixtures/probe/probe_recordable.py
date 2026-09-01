#!/usr/bin/env python3
"""Deterministic CLI fixture for supervised onboarding transcript recording."""

from __future__ import annotations

import sys


def _help() -> int:
    """Print the stable capability surface advertised by this fixture."""
    print(
        "probe-recordable: deterministic transcript fixture\n"
        "\n"
        "Usage: probe-recordable --model <name> --prompt <text>\n"
        "\n"
        "Options:\n"
        "  --model <name>    Select the model to use\n"
        "  --prompt <text>   Supply the prompt inline\n"
        "  --help            Show this message\n"
        "  --version         Show the version\n"
    )
    return 0


def main() -> int:
    """Run the fixture's deliberately small argument parser."""
    args = sys.argv[1:]
    if args == ["--version"]:
        print("probe-recordable 1.0.0")
        return 0
    if args == ["--help"]:
        return _help()
    if any(arg.startswith("-") and arg not in {"--model", "--prompt"} for arg in args):
        print("probe-recordable: unknown option", file=sys.stderr)
        return 2

    expected = {"--model", "--prompt"}
    if len(args) != 4 or set(args[::2]) != expected or any(not value for value in args[1::2]):
        print("probe-recordable: expected --model <name> --prompt <text>", file=sys.stderr)
        return 2
    print("probe-recordable: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
