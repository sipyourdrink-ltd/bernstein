#!/usr/bin/env python3
"""Deterministic probe fixture: help output contains --model and other common flags.

This fixture simulates a CLI whose --help output exposes the flags the
operator expects: ``--model <name>``, ``--prompt``, a ``--subcommand``
pattern, and an ``--env`` flag. The output is byte-stable across runs so
the content-addressed evidence hash is stable.
"""

import sys


def main() -> int:
    if "--version" in sys.argv:
        print("probe-with-model 2.0.0")
        return 0
    if "--help" in sys.argv:
        print(
            "probe-with-model: a deterministic probe fixture\n"
            "\n"
            "Usage: probe-with-model [OPTIONS] [ARGS]...\n"
            "\n"
            "Options:\n"
            "  --model <name>    Select the model to use\n"
            "  --prompt <text>   Supply the prompt inline\n"
            "  --subcommand <cmd>\n"
            "                    Choose a subcommand\n"
            "  --env <key=val>   Pass environment variables\n"
            "  --help            Show this message\n"
        )
        return 0
    print("probe-with-model: unknown invocation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
